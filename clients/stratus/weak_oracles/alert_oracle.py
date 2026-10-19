import json
import logging
import os
import subprocess
import time

import requests

from clients.stratus.weak_oracles.base_oracle import BaseOracle, OracleResult

logger = logging.getLogger("all.stratus.alert_oracle")

_SUSTAINED_SILENCE_SECONDS = 120
_POLL_INTERVAL_SECONDS = 10
_BUFFER_SECONDS = 30


def _get_benchmark_status() -> str:
    try:
        api_hostname = os.getenv("API_HOSTNAME", "localhost")
        api_port = os.getenv("API_PORT", "8000")
        response = requests.get(f"http://{api_hostname}:{api_port}/status", timeout=5)
        if response.status_code == 200:
            return response.json().get("stage", "unknown")
    except Exception:
        pass
    return "unknown"


class AlertOracle(BaseOracle):
    """Weak oracle that waits for matching Prometheus alerts to stop firing."""

    def __init__(
        self,
        namespace: str,
        sustained_silence_seconds: int = _SUSTAINED_SILENCE_SECONDS,
        poll_interval_seconds: int = _POLL_INTERVAL_SECONDS,
        buffer_seconds: int = _BUFFER_SECONDS,
        match_labels: dict[str, str] | None = None,
        recovery_timeout_seconds: int = 0,
    ):
        self.namespace = namespace
        self.sustained_silence_seconds = sustained_silence_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.buffer_seconds = buffer_seconds
        self.match_labels = match_labels
        self.recovery_timeout_seconds = recovery_timeout_seconds

    def _query_firing_alerts(self) -> list[dict] | None:
        """Returns list of firing alerts, or None if the cluster is being torn down."""
        # Use kubectl get --raw to proxy through the API server (plain HTTP, no WebSockets).
        proxy_path = "/api/v1/namespaces/observe/services/prometheus-server:80/proxy/api/v1/alerts"
        cmd = ["kubectl", "get", "--raw", proxy_path]
        try:
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=15)
            stderr = result.stderr or ""
            if result.returncode != 0:
                if "NotFound" in stderr or "not found" in stderr.lower():
                    logger.info("[AlertOracle] Prometheus not found (cluster teardown detected), stopping poll.")
                    return None
                logger.warning(f"Failed to query Prometheus alerts: exit {result.returncode}; stderr: {stderr!r}")
                if self.match_labels is not None:
                    raise RuntimeError(f"Prometheus alert query failed: {stderr}")
                return []
            payload = json.loads(result.stdout)
        except subprocess.TimeoutExpired as exc:
            logger.warning(f"Failed to query Prometheus alerts: {exc}")
            if self.match_labels is not None:
                raise RuntimeError("Prometheus alert query timed out") from exc
            return []
        except json.JSONDecodeError as exc:
            logger.warning(f"Failed to parse Prometheus alerts response: {exc}")
            if self.match_labels is not None:
                raise RuntimeError("Prometheus alert response was invalid") from exc
            return []
        if self.match_labels is not None and (
            payload.get("status") != "success" or not isinstance(payload.get("data", {}).get("alerts"), list)
        ):
            raise RuntimeError("Prometheus alert response was incomplete")

        return [
            alert
            for alert in payload.get("data", {}).get("alerts", [])
            if alert.get("state") == "firing"
            and alert.get("labels", {}).get("namespace") == self.namespace
            and all(alert.get("labels", {}).get(key) == value for key, value in (self.match_labels or {}).items())
        ]

    def wait_for_firing(self, timeout_seconds: int = 240) -> None:
        """Fail closed if an assigned alert never becomes firing before agent kickoff."""
        if self.match_labels is None:
            raise ValueError("wait_for_firing requires a specific alert")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            firing = self._query_firing_alerts()
            if firing is None:
                raise RuntimeError("Prometheus disappeared before the assigned alert fired")
            if firing:
                return
            time.sleep(self.poll_interval_seconds)
        raise TimeoutError(f"Assigned alert did not fire within {timeout_seconds}s: {self.match_labels}")

    def validate(self) -> OracleResult:
        logger.info(f"Waiting {self.buffer_seconds}s before checking alerts...")
        time.sleep(self.buffer_seconds)

        start = time.monotonic()
        silence_start = None
        while True:
            status = _get_benchmark_status()
            if status in ("tearing_down", "done"):
                logger.info(f"[AlertOracle] Benchmark is '{status}', stopping alert polling.")
                if self.match_labels is not None:
                    return OracleResult(success=False, issues=["Benchmark ended before alert recovery was verified"])
                break

            firing = self._query_firing_alerts()
            if firing is None:
                if self.match_labels is not None:
                    return OracleResult(
                        success=False, issues=["Prometheus disappeared before alert recovery was verified"]
                    )
                break
            if firing:
                silence_start = None
                if self.recovery_timeout_seconds and time.monotonic() - start < self.recovery_timeout_seconds:
                    logger.info("Assigned alert is still firing; waiting for the rate window to clear")
                    time.sleep(self.poll_interval_seconds)
                    continue
                names = ", ".join(a.get("labels", {}).get("alertname", "?") for a in firing)
                logger.info(f"Firing alerts in {self.namespace}: {names}")
                logger.info(f"[AlertOracle] FAIL — firing alerts detected in namespace '{self.namespace}': {names}")
                return OracleResult(success=False, issues=[f"Firing alerts: {names}"])

            if silence_start is None:
                silence_start = time.monotonic()
            if time.monotonic() - silence_start >= self.sustained_silence_seconds:
                break
            time.sleep(self.poll_interval_seconds)

        logger.info(
            f"[AlertOracle] PASS — no firing alerts detected in namespace '{self.namespace}' for {self.sustained_silence_seconds}s"
        )
        return OracleResult(success=True, issues=[])
