"""Run matched control/Geo-treatment pilots and fail closed on interference.

This script starts no agent and no LLM judge. Each arm uses the image in
SREGYM_HOTEL_CLOCK_SKEW_IMAGE, runs a 60-second post-trigger search window,
applies the problem's documented safe mitigation, and checks its deterministic
mitigation oracle. Run only on a disposable SREGym Kubernetes test cluster.
"""

import json
import subprocess
import sys
from pathlib import Path

SMOKE_SCRIPT = Path(__file__).with_name("smoke_clock_skew_no_agent.py")
COUNTERS = (
    "search_requests_total",
    "search_rate_attempts_total",
    "search_rate_timeouts_total",
    "rate_requests_received_total",
    "rate_requests_completed_total",
    "rate_requests_expired_total",
    "rate_requests_rejected_total",
)


def run_arm(*, treatment: bool) -> dict:
    command = [sys.executable, str(SMOKE_SCRIPT), "--measure-seconds", "60", "--verify-mitigation"]
    command += ["--noise-profile", "clock-skew-geo"] if treatment else ["--control"]
    arm = "treatment" if treatment else "control"
    print(f"PAIR_START {arm}", flush=True)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    records = {}
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        for marker in ("SMOKE_TOPOLOGY", "SMOKE_WINDOW", "SMOKE_ORACLE", "SMOKE_PREFLIGHT", "SMOKE_FINAL"):
            if line.startswith(marker + " "):
                records[marker] = json.loads(line[len(marker) + 1 :])
    if process.wait() != 0:
        raise RuntimeError(f"{arm} arm failed; see the preceding smoke output")
    if records.get("SMOKE_ORACLE", {}).get("success") is not True:
        raise RuntimeError(f"{arm} primary mitigation oracle did not pass")
    if treatment:
        preflight = records.get("SMOKE_PREFLIGHT", {}).get("preflight", {})
        if (
            preflight.get("status") != "passed"
            or preflight.get("geo_status_before") != 200
            or preflight.get("geo_status_after") != 502
        ):
            raise RuntimeError("Geo treatment symptom was not confirmed")
        if records.get("SMOKE_FINAL", {}).get("cleanup", {}).get("status") != "confirmed":
            raise RuntimeError("Geo treatment cleanup was not confirmed")
    topology = records.get("SMOKE_TOPOLOGY")
    if not topology or len(set(topology.get("digests", {}).values())) != 1:
        raise RuntimeError(f"{arm} has no single resolved image digest")
    if set(topology.get("endpoints", {})) != {"frontend", "search", "geo", "rate"}:
        raise RuntimeError(f"{arm} has incomplete critical-path Service endpoints")
    if any(len(addresses) != 1 for addresses in topology["endpoints"].values()):
        raise RuntimeError(f"{arm} has an unexpected critical-path endpoint count")
    window = records.get("SMOKE_WINDOW")
    if not window:
        raise RuntimeError(f"{arm} arm produced no 60-second window")
    window["topology"] = topology
    return window


def compare_windows(control: dict, treatment: dict) -> None:
    control_topology = control["topology"]
    treatment_topology = treatment["topology"]
    if control_topology["digests"] != treatment_topology["digests"]:
        raise AssertionError("control and Geo treatment used different resolved image digests")
    control_counts = {name: len(addresses) for name, addresses in control_topology["endpoints"].items()}
    treatment_counts = {name: len(addresses) for name, addresses in treatment_topology["endpoints"].items()}
    if control_counts != treatment_counts:
        raise AssertionError("control and Geo treatment had different critical-path endpoint counts")
    for arm, window in (("control", control), ("treatment", treatment)):
        workload = window["workload"]
        if workload["submitted"] < 450 or workload["completed"] < 450:
            raise AssertionError(f"{arm} did not deliver the protected 8 req/s workload")
        if workload["success_rate"] > 0.05:
            raise AssertionError(f"{arm} did not retain the primary search failure")
        if window["rate_queue_depth_before"] < 20 or window["rate_queue_depth_after"] < 20:
            raise AssertionError(f"{arm} Rate queue was not persistently backlogged")
        search_requests = window["counters"]["search_requests_total"]
        rate_attempts = window["counters"]["search_rate_attempts_total"]
        if search_requests < 400 or rate_attempts / search_requests < 2:
            raise AssertionError(f"{arm} lost the Search-to-Rate retry amplification")

    if abs(control["workload"]["success_rate"] - treatment["workload"]["success_rate"]) > 0.05:
        raise AssertionError("Geo treatment changed protected search success")
    for name in COUNTERS:
        baseline = control["counters"][name]
        observed = treatment["counters"][name]
        if baseline <= 0 or abs(observed - baseline) / baseline > 0.15:
            raise AssertionError(f"Geo treatment changed {name}: control={baseline}, treatment={observed}")
    print("PAIR_PASS Geo clock skew did not change the measured primary failure or mitigation outcome", flush=True)


if __name__ == "__main__":
    compare_windows(run_arm(treatment=False), run_arm(treatment=True))
