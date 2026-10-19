"""Bare-metal CPUFreq treatment for the Hotel Reservation selector case.

Design and required runtime gates: docs/agents/cpu-frequency-selector-design.md.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import paramiko
import requests
import yaml

from sregym.generators.noise.impl.recommendation_rpc import RecommendationRpcLoad, RpcWindow
from sregym.generators.workload.hotel_search import KubectlPortForward
from sregym.paths import BASE_DIR
from sregym.profile import is_svelte

CPU_FREQUENCY_PROFILE = "cpu-frequency-throttling"
CPU_FREQUENCY_CONTROL_PROFILE = "cpu-frequency-control"
ALLOWED_NODE_DAEMONSETS = {
    ("kube-system", "calico-node"),
    ("kube-system", "kube-proxy"),
    ("observe", "prometheus-prometheus-node-exporter"),
    ("chaos-mesh", "chaos-daemon"),
    ("openebs", "openebs-ndm"),
    ("openebs", "openebs-ndm-node-exporter"),
}
# Run on the selected worker only. CPUFreq policy directories are the kernel's
# unit of control; per-CPU symlinks can point to the same policy.
_POLICY_SCRIPT = r"""
import base64, glob, json, pathlib, sys
action = sys.argv[1]
data = json.loads(base64.b64decode(sys.argv[2]))
root = pathlib.Path('/sys/devices/system/cpu/cpufreq')
paths = sorted(root.glob('policy*'))
if not paths:
    raise RuntimeError('no CPUFreq policies')
def read(path):
    def field(name, optional=False):
        file = path / name
        if optional and not file.exists():
            return None
        return file.read_text().strip()
    return {
        'governor': field('scaling_governor'),
        'min': int(field('scaling_min_freq')),
        'max': int(field('scaling_max_freq')),
        'hardware_min': int(field('cpuinfo_min_freq')),
        'hardware_max': int(field('cpuinfo_max_freq')),
        'current': int(field('scaling_cur_freq')),
        'hardware_current': int(field('cpuinfo_cur_freq', True)) if field('cpuinfo_cur_freq', True) else None,
        'available': field('scaling_available_frequencies', True),
        'affected_cpus': field('affected_cpus'),
    }
if action in ('set', 'restore'):
    expected = {path.name for path in paths}
    if set(data) != expected:
        raise RuntimeError('CPUFreq policy set changed')
    for path in paths:
        old = read(path)
        values = data[path.name]
        cap = int(values['max'] if action == 'restore' else values)
        minimum = int(values['min']) if action == 'restore' else old['min']
        if not old['hardware_min'] <= minimum <= cap <= old['hardware_max']:
            raise RuntimeError('CPUFreq values outside hardware range')
    for path in paths:
        values = data[path.name]
        if action == 'restore':
            # Raise the maximum before the minimum, then restore the governor.
            (path / 'scaling_max_freq').write_text(str(int(values['max'])))
            (path / 'scaling_min_freq').write_text(str(int(values['min'])))
            (path / 'scaling_governor').write_text(values['governor'])
        else:
            (path / 'scaling_max_freq').write_text(str(int(values)))
elif action != 'read':
    raise RuntimeError('unknown CPUFreq action')
print(json.dumps({path.name: read(path) for path in paths}, sort_keys=True))
"""


@dataclass(frozen=True)
class WorkerIdentity:
    node_name: str
    host: str
    user: str
    pod_name: str
    pod_uid: str


def _resolve_user(value: str, variables: dict) -> str:
    match = re.fullmatch(r"\{\{\s*(\w+)\s*\}\}", value.strip())
    if not match:
        if "{{" in value:
            raise RuntimeError(f"unresolved inventory user: {value}")
        return value
    name = match.group(1)
    if name not in variables:
        raise RuntimeError(f"inventory variable {name} is missing")
    return str(variables[name])


class BareMetalCpuFreq:
    """Use an exact inventory/node match and preserve every CPUFreq policy."""

    def __init__(self, identity: WorkerIdentity):
        self.identity = identity
        self.client: paramiko.SSHClient | None = None
        self.original: dict[str, dict] | None = None

    @classmethod
    def identify(cls, kubectl, namespace: str) -> WorkerIdentity:
        api = kubectl.core_v1_api
        deployment = kubectl.apps_v1_api.read_namespaced_deployment("recommendation", namespace)
        labels = dict(deployment.spec.selector.match_labels or {})
        if not labels:
            raise RuntimeError("recommendation Deployment has no Pod selector")
        selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        pods = api.list_namespaced_pod(namespace, label_selector=selector).items
        ready = [
            pod
            for pod in pods
            if pod.status.phase == "Running"
            and pod.spec.node_name
            and any(c.type == "Ready" and c.status == "True" for c in pod.status.conditions or [])
        ]
        if len(ready) != 1:
            raise RuntimeError(f"expected one Ready recommendation Pod, found {len(ready)}")
        pod = ready[0]
        node_name = pod.spec.node_name
        node = api.read_node(node_name)
        if (node.spec.provider_id or "").startswith("kind://"):
            raise RuntimeError("CPU frequency treatment requires bare-metal workers, not Kind")
        node_labels = node.metadata.labels or {}
        if any(
            key in node_labels for key in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")
        ):
            raise RuntimeError("refusing to change a control-plane node")

        # The frequency cap applies to every Pod on this node. Verify every
        # namespace, including control-plane, discovery, ingress, and storage.
        colocated = api.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}").items
        prohibited = []
        for item in colocated:
            name = item.metadata.name
            if item.metadata.namespace == namespace and name == pod.metadata.name:
                continue
            owners = item.metadata.owner_references or []
            allowed_daemon = any(
                owner.kind == "DaemonSet" and (item.metadata.namespace, owner.name) in ALLOWED_NODE_DAEMONSETS
                for owner in owners
            )
            if not allowed_daemon:
                prohibited.append(f"{item.metadata.namespace}/{name}")
        if prohibited:
            raise RuntimeError(f"recommendation worker is not isolated: {sorted(prohibited)}")

        inventory_path = Path(BASE_DIR).parent / "scripts/ansible/inventory.yml"
        with inventory_path.open(encoding="utf-8") as stream:
            inventory = yaml.safe_load(stream)
        root = inventory.get("all", {})
        variables = root.get("vars", {})
        hosts = root.get("children", {}).get("worker_nodes", {}).get("hosts", {})
        matches = []
        for alias, config in hosts.items():
            host = str(config["ansible_host"])
            candidates = {alias, host, host.split(".")[0]}
            if node_name in candidates or node_name.split(".")[0] in candidates:
                matches.append((host, _resolve_user(str(config["ansible_user"]), variables)))
        if len(matches) != 1:
            raise RuntimeError(f"expected one exact inventory match for worker {node_name}, found {len(matches)}")
        host, user = matches[0]
        return WorkerIdentity(node_name, host, user, pod.metadata.name, pod.metadata.uid)

    def connect(self) -> None:
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        ssh.connect(self.identity.host, username=self.identity.user, timeout=10)
        self.client = ssh
        status, output, error = self._exec("hostname -s; hostname -f; systemd-detect-virt")
        names = output.splitlines()
        if status not in (0, 1) or len(names) < 3:
            raise RuntimeError(f"cannot verify bare-metal worker identity: {error.strip()}")
        if self.identity.node_name not in names[:2] and self.identity.node_name.split(".")[0] not in names[:2]:
            raise RuntimeError(f"SSH host does not identify as Kubernetes node {self.identity.node_name}")
        if names[2].strip() != "none":
            raise RuntimeError(f"worker is virtualized ({names[2].strip()})")
        self.original = self.read()
        if not self.original:
            raise RuntimeError("worker exposes no CPUFreq policies")

    def _exec(self, command: str) -> tuple[int, str, str]:
        if self.client is None:
            raise RuntimeError("SSH connection is not open")
        _, stdout, stderr = self.client.exec_command(command, timeout=30)
        status = stdout.channel.recv_exit_status()
        return status, stdout.read().decode(), stderr.read().decode()

    def _policy(self, action: str, values: dict | None = None) -> dict[str, dict]:
        payload = base64.b64encode(json.dumps(values or {}).encode()).decode()
        command = "sudo -n python3 -c " + shlex.quote(_POLICY_SCRIPT) + f" {shlex.quote(action)} {payload}"
        status, output, error = self._exec(command)
        if status != 0:
            raise RuntimeError(f"CPUFreq {action} failed: {error.strip()}")
        return json.loads(output)

    def read(self) -> dict[str, dict]:
        return self._policy("read")

    def set_caps(self, caps: dict[str, int]) -> dict[str, dict]:
        observed = self._policy("set", caps)
        for policy, cap in caps.items():
            if observed[policy]["max"] != cap:
                raise RuntimeError(f"CPUFreq cap for {policy} did not read back as requested")
        return observed

    def restore(self) -> None:
        if self.original is None:
            return
        observed = self._policy("restore", self.original)
        for name, values in self.original.items():
            if any(observed[name][field] != values[field] for field in ("min", "max", "governor")):
                raise RuntimeError(f"CPUFreq policy {name} did not return to its original state")

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None


def candidate_caps(original: dict[str, dict]) -> list[dict[str, int]]:
    """Try progressively lower caps without changing any minimum or governor."""
    candidates = []
    for fraction in (0.75, 0.5, 0.25, 0.0):
        caps = {}
        for name, policy in original.items():
            values = [int(item) for item in (policy["available"] or "").split() if item.isdigit()]
            floor = max(policy["min"], policy["hardware_min"])
            target = floor + int((policy["max"] - floor) * fraction)
            allowed = sorted(value for value in values if floor <= value <= policy["max"])
            caps[name] = min((value for value in allowed if value >= target), default=target) if allowed else target
        if any(caps[name] < original[name]["max"] for name in caps) and caps not in candidates:
            candidates.append(caps)
    return candidates


class CpuFrequencyTreatment:
    """Calibrate a real node cap and fail closed on every design gate."""

    measure_seconds = 60
    min_rpc_samples = 1000
    healthy_timeout_ratio = 0.01
    treatment_timeout_ratio = 0.10
    healthy_hotel_success = 0.99
    maximum_hotel_success_delta = 0.01

    def __init__(
        self,
        kubectl,
        namespace: str,
        problem_id: str,
        expected_node: str | None = None,
        problem=None,
        control: bool = False,
    ):
        if problem_id != "wrong_service_selector_hotel_reservation":
            raise RuntimeError("CPU frequency profile is limited to the Hotel Reservation selector problem")
        if is_svelte():
            raise RuntimeError("CPU frequency profile requires the full Prometheus deployment")
        self.kubectl = kubectl
        self.namespace = namespace
        self.expected_node = expected_node
        self.problem = problem
        self.control = control
        self.identity: WorkerIdentity | None = None
        self.host: BareMetalCpuFreq | None = None
        self.client: RecommendationRpcLoad | None = None
        self.prometheus: KubectlPortForward | None = None
        self.applied_caps: dict[str, int] | None = None
        self.evidence: dict = {
            "profile": CPU_FREQUENCY_CONTROL_PROFILE if control else CPU_FREQUENCY_PROFILE,
            "preflight": {"status": "not_started"},
        }

    def _prom_query(self, expression: str) -> list[dict]:
        if self.prometheus is None:
            raise RuntimeError("Prometheus tunnel is not running")
        port = self.prometheus.start()
        response = requests.get(
            f"http://127.0.0.1:{port}/api/v1/query",
            params={"query": expression},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed for {expression}")
        return payload["data"]["result"]

    def _node_frequency(self) -> float:
        if self.identity is None:
            raise RuntimeError("CPU frequency target is unknown")
        pods = self.kubectl.core_v1_api.list_namespaced_pod("observe").items
        exporters = [
            pod
            for pod in pods
            if pod.spec.node_name == self.identity.node_name
            and "node-exporter" in pod.metadata.name
            and pod.status.pod_ip
        ]
        if len(exporters) != 1:
            raise RuntimeError("target worker has no unique node-exporter Pod")
        ip = exporters[0].status.pod_ip
        series = self._prom_query("node_cpu_scaling_frequency_hertz")
        values = [float(row["value"][1]) for row in series if row["metric"].get("instance", "").startswith(ip + ":")]
        if not values:
            raise RuntimeError("agent-queryable node CPU frequency metric is missing")
        return sum(values) / len(values)

    def _client_metric_visible(self) -> None:
        series = self._prom_query(f'hotel_recommendation_rpc_timeout_ratio{{namespace="{self.namespace}"}}')
        if not series:
            raise RuntimeError("recommendation RPC timeout metric is not visible to the agent")

    def _endpoints(self) -> dict[str, tuple[str, ...]]:
        result = {}
        for name in ("frontend", "search", "geo", "rate", "reservation", "profile", "recommendation"):
            endpoint = self.kubectl.core_v1_api.read_namespaced_endpoints(name, self.namespace)
            result[name] = tuple(
                sorted(address.ip for subset in endpoint.subsets or [] for address in subset.addresses or [])
            )
        return result

    def _faulty_selector(self) -> dict[str, str]:
        service = self.kubectl.core_v1_api.read_namespaced_service("frontend", self.namespace)
        selector = dict(service.spec.selector or {})
        if selector.get("current_service_name") != "frontend" or not selector.get("io.kompose.service") == "frontend":
            raise RuntimeError("the expected frontend selector fault is not active")
        if self._endpoints()["frontend"]:
            raise RuntimeError("frontend still has endpoints despite the injected selector")
        return selector

    def _oracle_failure(self) -> str:
        if self.problem is None:
            raise RuntimeError("CPU frequency preflight requires the existing problem oracle")
        verdict = self.problem.mitigation_oracle.evaluate()
        if verdict.get("success") or verdict.get("reason") != "no_ready_endpoints":
            raise RuntimeError(f"selector-fault oracle changed outcome: {verdict}")
        return verdict["reason"]

    def _replace_selector(self, selector: dict[str, str]) -> None:
        self.kubectl.core_v1_api.patch_namespaced_service(
            "frontend",
            self.namespace,
            [{"op": "replace", "path": "/spec/selector", "value": selector}],
        )

    def _wait_endpoints(self, present: bool) -> tuple[str, ...]:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            current = self._endpoints()["frontend"]
            if bool(current) == present:
                return current
            time.sleep(1)
        raise TimeoutError(f"frontend endpoints did not become {'present' if present else 'empty'}")

    def _hotel_success_while_repaired(self, faulty_selector: dict[str, str]) -> tuple[float, tuple[str, ...]]:
        """Brief preflight repair; always restore the original primary fault."""
        corrected = {key: value for key, value in faulty_selector.items() if key != "current_service_name"}
        forward = KubectlPortForward(self.namespace, "frontend", 5000)
        try:
            self._replace_selector(corrected)
            endpoint_ids = self._wait_endpoints(True)
            port = forward.start()
            url = f"http://127.0.0.1:{port}/hotels?inDate=2015-04-09&outDate=2015-04-10&lat=38.0235&lon=-122.095"

            def probe() -> bool:
                try:
                    return requests.get(url, timeout=4).status_code == 200
                except requests.RequestException:
                    return False

            with ThreadPoolExecutor(max_workers=32) as pool:
                futures = []
                next_at = time.monotonic()
                deadline = next_at + self.measure_seconds
                while time.monotonic() < deadline:
                    futures.append(pool.submit(probe))
                    next_at += 0.1
                    time.sleep(max(0, min(next_at - time.monotonic(), 0.1)))
                successes = sum(future.result() for future in futures)
            if self.problem is None or not self.problem.mitigation_oracle.evaluate().get("success"):
                raise RuntimeError("selector repair did not pass the existing mitigation oracle")
            return successes / len(futures), endpoint_ids
        finally:
            try:
                forward.stop()
            finally:
                self._replace_selector(faulty_selector)
                self._wait_endpoints(False)

    @staticmethod
    def _assert_healthy_rpc(window: RpcWindow) -> None:
        if window.total < CpuFrequencyTreatment.min_rpc_samples:
            raise RuntimeError(f"too few recommendation RPC samples: {window.total}")
        if window.other_errors or window.timeout_ratio > CpuFrequencyTreatment.healthy_timeout_ratio:
            raise RuntimeError(f"recommendation RPC baseline is unhealthy: {window}")

    def start(self) -> dict:
        self.identity = BareMetalCpuFreq.identify(self.kubectl, self.namespace)
        if self.expected_node and self.identity.node_name != self.expected_node:
            raise RuntimeError("recommendation did not land on the requested CPU frequency worker")
        self.host = BareMetalCpuFreq(self.identity)
        try:
            faulty_selector = self._faulty_selector()
            baseline_oracle_reason = self._oracle_failure()
            self.host.connect()
            self.prometheus = KubectlPortForward("observe", "prometheus-server", 80)
            self.prometheus.start()
            self.client = RecommendationRpcLoad(self.namespace)
            self.client.start()
            warmup = self.client.measure(15)
            if warmup.other_errors:
                raise RuntimeError("recommendation RPC warmup has transport errors")
            self.client.timeout_seconds = max(0.01, 2.0 * max(warmup.p99_seconds, 0.001))
            baseline_rpc = self.client.measure(self.measure_seconds)
            self._assert_healthy_rpc(baseline_rpc)
            baseline_frequency = self._node_frequency()
            self._client_metric_visible()
            baseline_hotel, baseline_repaired_endpoints = self._hotel_success_while_repaired(faulty_selector)
            if baseline_hotel < self.healthy_hotel_success:
                raise RuntimeError(f"hotel-search control is unhealthy: {baseline_hotel:.3f}")
            healthy_repaired_rpc = self.client.window(self.measure_seconds)
            self._assert_healthy_rpc(healthy_repaired_rpc)
            baseline_endpoints = self._endpoints()
            self.evidence["baseline"] = {
                "original_policies": self.host.original,
                "rpc_total": baseline_rpc.total,
                "rpc_timeouts": baseline_rpc.timeouts,
                "rpc_p99_seconds": baseline_rpc.p99_seconds,
                "client_timeout_seconds": self.client.timeout_seconds,
                "hotel_success": baseline_hotel,
                "node_frequency_hz": baseline_frequency,
                "endpoints": baseline_endpoints,
                "fault_oracle_reason": baseline_oracle_reason,
            }

            if self.control:
                self.evidence["preflight"] = {"status": "passed"}
                self.evidence["target"] = {
                    "node": self.identity.node_name,
                    "pod": self.identity.pod_name,
                    "uid": self.identity.pod_uid,
                }
                return self.evidence

            for caps in candidate_caps(self.host.original):
                try:
                    observed = self.host.set_caps(caps)
                    if any(observed[name]["governor"] != old["governor"] for name, old in self.host.original.items()):
                        raise RuntimeError("CPU governor changed during cap application")
                    time.sleep(5)
                    rpc = self.client.measure(self.measure_seconds)
                    current_frequency = self._node_frequency()
                    if rpc.total < self.min_rpc_samples or rpc.other_errors:
                        raise RuntimeError(f"recommendation RPC treatment has insufficient or invalid samples: {rpc}")
                    if current_frequency >= baseline_frequency or rpc.timeout_ratio < self.treatment_timeout_ratio:
                        continue
                    if self._endpoints() != baseline_endpoints:
                        raise RuntimeError("CPU frequency treatment changed primary Service endpoints")
                    if self._oracle_failure() != baseline_oracle_reason:
                        raise RuntimeError("CPU frequency treatment changed the primary fault oracle")
                    hotel_success, repaired_endpoints = self._hotel_success_while_repaired(faulty_selector)
                    if repaired_endpoints != baseline_repaired_endpoints:
                        raise RuntimeError("CPU frequency treatment changed repaired frontend endpoints")
                    if (
                        hotel_success < self.healthy_hotel_success
                        or baseline_hotel - hotel_success > self.maximum_hotel_success_delta
                    ):
                        raise RuntimeError("CPU frequency treatment changed protected hotel-search behavior")
                    repaired_rpc = self.client.window(self.measure_seconds)
                    if (
                        repaired_rpc.total < self.min_rpc_samples
                        or repaired_rpc.timeout_ratio < self.treatment_timeout_ratio
                    ):
                        raise RuntimeError("recommendation timeout symptom disappeared after selector repair")
                    self._client_metric_visible()
                    self.applied_caps = caps
                    self.evidence.update(
                        preflight={"status": "passed"},
                        target={
                            "node": self.identity.node_name,
                            "pod": self.identity.pod_name,
                            "uid": self.identity.pod_uid,
                        },
                        treatment={
                            "caps_khz": caps,
                            "observed_policies": observed,
                            "node_frequency_hz": current_frequency,
                            "rpc_total": rpc.total,
                            "rpc_timeouts": rpc.timeouts,
                            "repaired_rpc_total": repaired_rpc.total,
                            "repaired_rpc_timeouts": repaired_rpc.timeouts,
                            "hotel_success": hotel_success,
                        },
                    )
                    return self.evidence
                finally:
                    if self.applied_caps is None:
                        self.host.restore()
            raise RuntimeError("no CPUFreq cap produced the required isolated recommendation timeout")
        except Exception:
            self.evidence["preflight"] = {"status": "failed"}
            self.stop()
            raise

    def stop(self) -> dict:
        errors = []
        if self.identity is not None:
            try:
                pod = self.kubectl.core_v1_api.read_namespaced_pod(self.identity.pod_name, self.namespace)
                self.evidence.setdefault("target", {})["changed_before_cleanup"] = (
                    pod.metadata.uid != self.identity.pod_uid or pod.spec.node_name != self.identity.node_name
                )
            except Exception:
                self.evidence.setdefault("target", {})["changed_before_cleanup"] = None
        if self.host is not None:
            try:
                before_restore = self.host.read()
                if self.applied_caps is not None:
                    self.evidence.setdefault("treatment", {})["cap_changed_before_cleanup"] = any(
                        before_restore.get(name, {}).get("max") != cap for name, cap in self.applied_caps.items()
                    )
                if not self.control:
                    self.host.restore()
                self.evidence["cleanup"] = {"status": "confirmed", "policies": self.host.read()}
                self.host.close()
                self.host = None
            except Exception as exc:
                errors.append(f"CPUFreq restoration: {exc}")
        if self.client is not None:
            try:
                self.client.stop()
                self.client = None
            except Exception as exc:
                errors.append(f"recommendation RPC client: {exc}")
        if self.prometheus is not None:
            self.prometheus.stop()
            self.prometheus = None
        if errors:
            self.evidence["cleanup"] = {"status": "incomplete", "errors": errors}
            raise RuntimeError("; ".join(errors))
        return self.evidence
