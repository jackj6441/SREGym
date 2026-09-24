# Hotel Reservation clock-skew noise pilot

This is the current design for pairing `clock-skew` with
`search_rate_retry_collapse_hotel_reservation`. The older two-container
observer design is retained only in [the historical note](clock-skew-noise.md).
Read the [case-specific design](agents/clock-skew-search-rate-design.md) and
[noise-case requirements](agents/noise-case-design-requirements.md) before
changing the profile.

## What changes

The same timestamp-aware Hotel Reservation application image is used for both
arms. The recommendation service stamps each gRPC result with its generation
time; frontend rejects results more than one minute ahead of its clock.
`/hotels` starts a bounded, asynchronous recommendation lookup on every tenth
request. Failure of this optional branch is logged but cannot change the
hotel-search response. When TimeChaos shifts only the existing recommendation
Pod's application container forward five minutes, `/recommendations` returns
an error and sampled `/hotels` requests produce frontend warnings. The
primary search/rate fault must remain unchanged.

The profile injects once, targets the exact existing recommendation Pod, and
is cleaned up only after mitigation validation. No observer Pod is created.
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
frontend/recommendation node or use a pullable, immutable image reference.

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
