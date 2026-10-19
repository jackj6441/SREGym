"""No-agent CloudLab smoke test for the real CPU-frequency noise profile.

Requires an already prepared full SREGym cluster with Prometheus in `observe`.
This program does not run an agent or call a judge model.
"""

import argparse
import json

from sregym.conductor.problems.wrong_service_selector import WrongServiceSelector
from sregym.generators.noise.impl.cpu_frequency import CpuFrequencyTreatment
from sregym.service.kubectl import KubeCtl


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-node", required=True, help="Dedicated Kubernetes worker node name")
    args = parser.parse_args()
    problem = WrongServiceSelector(app_name="hotel_reservation")
    problem.app.cpu_frequency_worker_node = args.worker_node
    treatment = None
    results = {}
    deployed = False
    injected = False
    try:
        problem.app.deploy()
        deployed = True
        problem.app.start_workload()
        problem.inject_fault()
        injected = True
        problem.validate_fault_preflight()
        for name, control in (("control", True), ("treatment", False)):
            treatment = CpuFrequencyTreatment(
                KubeCtl(),
                problem.namespace,
                "wrong_service_selector_hotel_reservation",
                args.worker_node,
                problem,
                control=control,
            )
            treatment.start()
            results[name] = treatment.stop()
            treatment = None
        control_baseline = results["control"]["baseline"]
        treatment_baseline = results["treatment"]["baseline"]
        if control_baseline["endpoints"] != treatment_baseline["endpoints"]:
            raise RuntimeError("matched control and treatment have different Service endpoints")
        if control_baseline["fault_oracle_reason"] != treatment_baseline["fault_oracle_reason"]:
            raise RuntimeError("matched control and treatment have different primary fault outcomes")
        if (
            control_baseline["hotel_success"] - results["treatment"]["treatment"]["hotel_success"]
            > CpuFrequencyTreatment.maximum_hotel_success_delta
        ):
            raise RuntimeError("treatment changed /hotels success beyond the matched control limit")
        print(json.dumps(results, indent=2, sort_keys=True))
    finally:
        errors = []
        if treatment is not None:
            try:
                treatment.stop()
            except Exception as exc:
                errors.append(f"CPUFreq cleanup: {exc}")
        if injected:
            try:
                problem.recover_fault()
            except Exception as exc:
                errors.append(f"selector recovery: {exc}")
        if deployed:
            try:
                problem.app.cleanup()
            except Exception as exc:
                errors.append(f"app cleanup: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))


if __name__ == "__main__":
    main()
