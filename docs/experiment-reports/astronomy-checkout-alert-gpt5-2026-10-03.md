# Astronomy Shop checkout alert experiment (GPT-5, 2026-10-03)

## Setup and success criterion

Six mitigation-only attempts ran on CloudLab `ms1008.utah.cloudlab.us` in the
order control 1, noise 1, control 2, noise 2, control 3, noise 3. All used the
same full deployment profile, workload, Stratus agent, `gpt-5` mitigation
model, `gpt-4o-mini` judge, and 1800-second agent timeout. Each attempt
deployed a fresh Astronomy Shop and injected the existing
`incorrect_port_assignment` fault. The noise arm additionally used
`astronomy-ad-image`.

The agent's system and user messages named only the firing
`HighRequestErrorRate` for `POST /api/checkout`. The six initial prompts did
not contain `8082`, `PRODUCT_CATALOG_ADDR`, the Product Catalog address, or the
ad fault. Success required the exact checkout alert to stop firing for 120
seconds and the benchmark's Product Catalog dependency oracle to pass. Other
alerts were excluded from the weak oracle.

## Results

| Attempt | Arm | Target alert cleared | Main mitigation oracle | Agent run (s) | Noise gate / cleanup |
| --- | --- | --- | --- | ---: | --- |
| 1 | Control | Yes | Pass | 133.719 | — |
| 1 | Ad image noise | Yes | Pass | 176.700 | Passed / confirmed |
| 2 | Control | Yes | Pass | 141.653 | — |
| 2 | Ad image noise | Yes | Pass | 120.300 | Passed / confirmed |
| 3 | Control | Yes | Pass | 155.396 | — |
| 3 | Ad image noise | Yes | Pass | 214.881 | Passed / confirmed |

All six runs have `run_status=complete` and `Mitigation.success=True`. The
driver logged a passing targeted `AlertOracle` before submitting in each run.
All six agents independently changed checkout's Product Catalog address back
to `product-catalog:8080`. In noise attempt 1, the agent also added an
unnecessary 8082 Service port to Product Catalog after correcting checkout.
This extra change did not affect the stated alert and dependency-oracle
success criteria, but it is a mitigation side effect worth tracking.

The agent-run times come from each Stratus output CSV's `time` field, measured
before weak-oracle validation. Control times were 133.719–155.396 seconds and
noise times were 120.300–214.881 seconds. With three attempts per arm, they do
not establish a speed effect. The observed success result is 3/3 in both arms.
The benchmark's mitigation-stage duration includes waiting for the alert to
fire in the control arm, so it must not be treated as agent execution time.

## Noise isolation evidence

Each noise attempt started with the checkout alert firing, a failing checkout
dependency, and real checkout, cart, and ad traffic. The profile then created
a new ad Pod with `ImagePullBackOff` and waited for its `PodStatusError` to
fire. The original Ready ad Pod kept serving. The bad-image Pod still existed
before cleanup, and cleanup restored the original ad image and Ready replica.

| Noise attempt | Checkout requests/s before → after | Checkout failure ratio before → after | Ad requests/s before → after |
| --- | ---: | ---: | ---: |
| 1 | 0.0476 → 0.0519 | 1.000 → 1.001 | 0.0656 → 0.1195 |
| 2 | 0.0172 → 0.0381 | 1.001 → 1.000 | 0.0612 → 0.1079 |
| 3 | 0.0190 → 0.0178 | 1.000 → 1.001 | 0.0674 → 0.1164 |

Ratios slightly above 1 come from Prometheus rate extrapolation. In each
attempt, all recorded primary-path and ad Service endpoint sets were identical
before and after noise; both cart control endpoints had zero failures; the
checkout alert stayed firing; and the checkout dependency oracle still failed
before the agent started. The profile's automatic non-interference gate passed
all three times.

## Calibration and cleanup

An earlier calibration attempt was intentionally stopped and excluded: the
agent repaired checkout, but the original weak oracle checked the alert before
its two-minute rate window plus one-minute firing hold could clear, then
rolled back the repair. The targeted oracle now allows up to four minutes for
clearance and then requires 120 seconds of silence. Its local and CloudLab
tests passed before the six formal attempts.

After the final attempt, the CloudLab node was Ready; the experiment's
Astronomy Shop and `observe` namespaces were removed by normal benchmark
cleanup. The original `~/SREGym` checkout remained clean. A Docker egress
proxy created on 2026-09-28 predates these attempts and was left untouched.

The raw benchmark and agent CSV results, ATIF trajectories, phase ledgers, and noise evidence are
copied locally under `.scratch/astronomy-alert-noise/formal-runs/`. The
extracted weak-oracle and benchmark result log lines are in
`formal-runs/oracle_verification.txt`. The run directories are `1003_2011`,
`1003_2023`, `1003_2040`, `1003_2053`, `1003_2109`, and `1003_2121`, in the
same order as the results table. The isolated CloudLab test checkout is
`~/SREGym-checkout-alert-test-20261003`.
