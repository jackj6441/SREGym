"""Verify the Hotel Reservation clock-skew case without launching an agent or judge.

Run only on a disposable SREGym Kubernetes test cluster. The conductor owns
deployment and cleanup; this script never submits diagnosis or mitigation.
"""

import asyncio
import json
import os

from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.generators.noise.manager import get_noise_manager
from sregym.profile import set_profile

PROBLEM_ID = "search_rate_retry_collapse_hotel_reservation"
GO_SERVICES = ("frontend", "geo", "profile", "rate", "recommendation", "reservation", "search", "user")


async def main() -> None:
    expected_image = os.environ["SREGYM_HOTEL_CLOCK_SKEW_IMAGE"]
    set_profile("full")
    conductor = Conductor(ConductorConfig(enable_noise=True, noise_profile="clock-skew", noise_duration_seconds=7200))
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
        evidence = get_noise_manager().evidence_snapshot()
        print("SMOKE_PREFLIGHT", json.dumps(evidence, sort_keys=True), flush=True)
        if evidence["preflight"]["status"] != "passed":
            raise AssertionError("clock-skew preflight did not pass")
    finally:
        conductor.finish_problem_in_background()
        await conductor.wait_for_submission_work(timeout=300)
        print("SMOKE_FINAL", json.dumps(get_noise_manager().evidence_snapshot(), sort_keys=True), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
