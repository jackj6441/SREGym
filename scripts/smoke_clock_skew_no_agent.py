"""Verify the Hotel Reservation clock-skew case without launching an agent or judge.

Run only on a disposable SREGym Kubernetes test cluster. The conductor owns
deployment and cleanup; this script never submits diagnosis or mitigation.
"""

import argparse
import asyncio
import json
import os
from dataclasses import asdict

from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.generators.noise.impl.clock_skew import NOISE_PROFILES
from sregym.generators.noise.manager import get_noise_manager
from sregym.profile import set_profile

PROBLEM_ID = "search_rate_retry_collapse_hotel_reservation"
GO_SERVICES = ("frontend", "geo", "profile", "rate", "recommendation", "reservation", "search", "user")
WINDOW_COUNTERS = (
    "search_requests_total",
    "search_rate_attempts_total",
    "search_rate_timeouts_total",
    "rate_requests_received_total",
    "rate_requests_completed_total",
    "rate_requests_expired_total",
    "rate_requests_rejected_total",
)


async def main(noise_profile: str | None, measure_seconds: int = 0, verify_mitigation: bool = False) -> None:
    expected_image = os.environ["SREGYM_HOTEL_CLOCK_SKEW_IMAGE"]
    set_profile("full")
    conductor = Conductor(
        ConductorConfig(
            enable_noise=noise_profile is not None, noise_profile=noise_profile, noise_duration_seconds=7200
        )
    )
    conductor.problem_id = PROBLEM_ID
    try:
        result = await conductor.start_problem()
        print("SMOKE_START", result, flush=True)
        images = {}
        for name in GO_SERVICES:
            deployment = conductor.kubectl.apps_v1_api.read_namespaced_deployment(
                name=name, namespace="hotel-reservation"
            )
            images[name] = next(
                container.image
                for container in deployment.spec.template.spec.containers
                if container.name == f"hotel-reserv-{name}"
            )
        print("SMOKE_IMAGES", json.dumps(images, sort_keys=True), flush=True)
        if set(images.values()) != {expected_image}:
            raise AssertionError("Hotel Reservation Go-service images differ")
        if noise_profile is not None:
            evidence = get_noise_manager().evidence_snapshot()
            print("SMOKE_PREFLIGHT", json.dumps(evidence, sort_keys=True), flush=True)
            if evidence["preflight"]["status"] != "passed":
                raise AssertionError("clock-skew preflight did not pass")
        if measure_seconds:
            workload = conductor.problem.workload
            before = workload.metrics.snapshot()
            await asyncio.sleep(measure_seconds)
            after = workload.metrics.snapshot()
            observed = workload.snapshot(measure_seconds)
            window = {
                "profile": noise_profile or "control",
                "workload": asdict(observed),
                "rate_queue_depth_before": before["rate_queue_depth"],
                "rate_queue_depth_after": after["rate_queue_depth"],
                "counters": {name: after[name] - before[name] for name in WINDOW_COUNTERS},
            }
            print("SMOKE_WINDOW", json.dumps(window, sort_keys=True), flush=True)
        if verify_mitigation:
            problem = conductor.problem
            problem._apply_mitigated_policy()
            problem._wait_for_rollouts()
            verdict = problem.mitigation_oracle.evaluate()
            print("SMOKE_ORACLE", json.dumps(verdict, sort_keys=True), flush=True)
            if not verdict.get("success"):
                raise AssertionError("primary mitigation oracle did not pass")
    finally:
        conductor.finish_problem_in_background()
        await conductor.wait_for_submission_work(timeout=300)
        print("SMOKE_FINAL", json.dumps(get_noise_manager().evidence_snapshot(), sort_keys=True), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--noise-profile", choices=NOISE_PROFILES, default="clock-skew")
    parser.add_argument("--control", action="store_true", help="Run the primary problem without noise")
    parser.add_argument("--measure-seconds", type=int, default=0)
    parser.add_argument(
        "--verify-mitigation", action="store_true", help="Apply safe primary mitigation and run its oracle"
    )
    args = parser.parse_args()
    if args.measure_seconds < 0:
        parser.error("--measure-seconds must be nonnegative")
    asyncio.run(main(None if args.control else args.noise_profile, args.measure_seconds, args.verify_mitigation))
