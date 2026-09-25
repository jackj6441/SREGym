# Hotel Reservation clock-skew noise pilot

This is the current design for pairing `clock-skew` with
`search_rate_retry_collapse_hotel_reservation`. The older two-container
observer design is retained only in [the historical note](clock-skew-noise.md).
Read the [case-specific design](agents/clock-skew-search-rate-design.md) and
[noise-case requirements](agents/noise-case-design-requirements.md) before
changing the profile.

## What changes

The same timestamp-aware Hotel Reservation application image is used for all
eight Go services in both arms (frontend, geo, profile, rate, recommendation,
reservation, search, user). This avoids an obvious image-version clue while
leaving database/cache sidecars unchanged. The recommendation service stamps each gRPC result with its generation
time; frontend rejects results more than one minute ahead of its clock.
`/hotels` starts a bounded, asynchronous recommendation lookup on every tenth
request. Failure of this optional branch is logged but cannot change the
hotel-search response. When TimeChaos shifts only the existing recommendation
Pod's application container forward five minutes, `/recommendations` returns
an error and sampled `/hotels` requests produce frontend warnings. The
primary search/rate fault must remain unchanged.

The profile injects once, targets the exact existing recommendation Pod, and
is cleaned up only after mitigation validation. No observer Pod is created.
If an agent rolls or replaces that Pod, the old exact-Pod treatment stops
affecting the replacement. Record this as target replacement during the agent
window and inspect the trajectory to determine whether the agent caused it;
do not label it manager reinjection or a manager lifecycle failure.
TimeChaos duration is a treatment TTL, not a reinjection interval. Its default
is 7200 seconds; choose a longer explicit TTL if an attempt can last longer.

## CloudLab setup and manual pilot

The current CloudLab host has one `amd64` node with the Docker Kubernetes
runtime. After obtaining a checkout containing both the parent repository and
the updated `SREGym-applications` submodule, run on CloudLab:

```bash
cd ~/SREGym
docker build -t sregym/hotel-reservation:20260924.1 SREGym-applications/hotelReservation
docker image inspect sregym/hotel-reservation:20260924.1 --format '{{.Id}}'
```

`--force-build` in `main.py` builds the agent image, **not** this application
image. The command above is required. If the Kubernetes cluster later has
more than one node, make this application image available on every possible
Hotel Reservation Go-service node or use a pullable, immutable image reference.

The user runs the attempts, not Codex. From the CloudLab shell, source the
existing credential file without printing it, then set the non-secret image
reference for both arms:

```bash
cd ~/SREGym
source ~/.config/sregym/env
export SREGYM_HOTEL_CLOCK_SKEW_IMAGE=sregym/hotel-reservation:20260924.1
```

First run one **new no-noise** control on the timestamp-aware image:

```bash
.venv/bin/python main.py \
  --force-build \
  --problem search_rate_retry_collapse_hotel_reservation \
  --stages diagnosis mitigation \
  --agent codex --model gpt-6-sol --judge-model gpt-4o-mini \
  --profile full --agent-timeout 1800 --n-attempts 1
```

Only after that control completes, run one treatment with the same settings:

```bash
.venv/bin/python main.py \
  --force-build \
  --problem search_rate_retry_collapse_hotel_reservation \
  --stages diagnosis mitigation \
  --agent codex --model gpt-6-sol --judge-model gpt-4o-mini \
  --profile full --agent-timeout 1800 --n-attempts 1 \
  --noise --noise-profile clock-skew --noise-duration-seconds 7200
```

Do not count a treatment attempt if `noise_preflight` is absent or failed.
The preflight requires: a healthy direct recommendation response before
TimeChaos; a future-timestamp recommendation failure afterward; a sampled
frontend warning; the same primary Service endpoint identities; and a still
backlogged, failing protected search workload. Inability to verify the Go
process's actual clock effect aborts setup.

After both attempts finish, inspect their `results/<run-id>/codex/` folders.
For a clock-skew run, the host-only
`results/<run-id>/codex/search_rate_retry_collapse_hotel_reservation/noise_evidence_attempt1.json`
records verified preflight statuses/primary metrics, target Pod UID,
TimeChaos name, target identity at cleanup, and cleanup status. It is outside
the agent's `/logs` mount and contains no environment variables or credentials.
`changed_before_cleanup: true` proves a target identity change, not who
caused it; use the trajectory to distinguish agent action from cluster churn.
Provide the two run IDs for trace analysis. The old `0924_1147` run is useful
historical context but is not a matched control because it used the previous
application image. A single new pair is a feasibility check, not an estimate
of model error probability.

After the noise attempt, verify cleanup without reading credentials:

```bash
kubectl get timechaos -n chaos-mesh
kubectl get pods -n hotel-reservation
```

The TimeChaos object should be gone. The application namespace may already
have been removed by normal final cleanup. If TimeChaos remains, do not begin
another attempt until its cleanup state is diagnosed.

Before the next agent run, a no-agent CloudLab smoke check should verify one
new deploy uses the same image in all eight Go Deployments, normal
`/recommendations` returns 200, the primary fault is active, TimeChaos makes
`/recommendations` return the timestamp-specific 502 without improving
`/hotels`, and cleanup removes TimeChaos. Codex may perform this smoke check;
the user runs the matched Sol attempts and later Astra attempts.
The repeatable smoke command is:

```bash
cd ~/SREGym
export SREGYM_HOTEL_CLOCK_SKEW_IMAGE=sregym/hotel-reservation:20260924.1
.venv/bin/python scripts/smoke_clock_skew_no_agent.py
```

The September 25 CloudLab smoke run passed: all eight Go Deployment images
matched, recommendation changed from HTTP 200 to the expected HTTP 502, the
primary queue stayed at 255 with search success 0.0 before and after noise,
the target Pod UID stayed unchanged, and final cleanup removed TimeChaos and
the application namespace. This is infrastructure validation, not an agent
diagnosis/mitigation result.
