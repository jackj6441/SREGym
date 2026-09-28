# Geo clock-skew noise pilot

`clock-skew-geo` is a separate experimental profile paired with
`search_rate_retry_collapse_hotel_reservation`. It does not replace the
existing `clock-skew` Recommend profile or its application image. Read the
[Geo design and stop conditions](agents/clock-skew-geo-search-rate-design.md)
and [noise-case requirements](agents/noise-case-design-requirements.md) first.

The new image adds a read-only `GET /nearby-hotels?lat=38.0235&lon=-122.095`
frontend operation. Geo's normal `Nearby` result includes a generation-time
gRPC header. Frontend validates that header only for the new route and for a
bounded, asynchronous lookup from every tenth `/hotels`; Search continues to
consume Geo hotel IDs without timestamp validation. With normal clocks, the
new route should return HTTP 200 and hotel IDs. TimeChaos advances only the
existing Geo application container by five minutes; the route should return
HTTP 502 with an `ahead of frontend clock` error, and sampled `/hotels`
requests should leave optional frontend warnings. The treatment is a genuine
Geo data-validity fault, not a new observer Pod. Geo must remain Ready.

Geo is **parallel to Rate under Search**, not downstream of Rate. Because Geo
is on `/hotels`'s critical path, this pilot is invalid if the process-level
clock change alters the main search response or Search/Rate fault behavior.
The preflight checks endpoint identity, Rate queue, and protected search
success. A matched no-agent control and treatment comparison is still
required before any paid agent run.

## Build and smoke check

On a disposable CloudLab cluster, from a checkout containing this parent repo
and its updated `SREGym-applications` submodule, build a **new tag**; do not
overwrite `sregym/hotel-reservation:20260924.1`:

```bash
cd ~/SREGym
docker build -t sregym/hotel-reservation:geo-clock-skew-1 SREGym-applications/hotelReservation
docker image inspect sregym/hotel-reservation:geo-clock-skew-1 --format '{{.Id}}'
```

Use the same image reference in the no-noise and Geo-treatment arms. The paired
runner also compares the resolved SHA-256 image ID of all eight running Go
containers across arms, because the local tag can otherwise be rebuilt. The
existing `SREGYM_HOTEL_CLOCK_SKEW_IMAGE` variable is an image selector for
this primary problem; its name is historical and does not choose the noise
profile. Source the existing credential file without printing it:

```bash
cd ~/SREGym
source ~/.config/sregym/env
export SREGYM_HOTEL_CLOCK_SKEW_IMAGE=sregym/hotel-reservation:geo-clock-skew-1
.venv/bin/python scripts/smoke_clock_skew_no_agent.py --noise-profile clock-skew-geo
```

This is a **no-agent** infrastructure check. It must report all eight Go
Deployment images equal, `preflight.status=passed`, `geo_status_before=200`,
`geo_status_after=502`, an unchanged failing primary path, and
`cleanup.status=confirmed`. Confirm the TimeChaos object and application
namespace are gone before another attempt. If the Go process does not observe
the offset, the route does not change specifically because of a future
timestamp, or cleanup cannot be confirmed, stop; do not call it a successful
treatment.

Before a formal agent run, measure matched 60-second no-agent post-trigger
windows in both arms, using the same image, primary workload, and cluster
configuration. Compare `/hotels` success, Search→Rate attempts, Rate
receives/timeouts/rejections, Rate queue, Frontend/Search/Geo/Rate endpoints,
and the primary fault oracle outcome. Also verify that after safe primary
mitigation, `/hotels` recovers in both arms. A difference beyond normal
run-to-run variation, any extra search failure caused by Geo, or an oracle
change invalidates this placement. Jaeger traces were unavailable in the
2026-09-27 probe, so trace absence must not be interpreted as no Geo traffic.
The automated no-agent comparison (two full deployments and cleanups) is:

```bash
cd ~/SREGym
source ~/.config/sregym/env
export SREGYM_HOTEL_CLOCK_SKEW_IMAGE=sregym/hotel-reservation:geo-clock-skew-1
.venv/bin/python scripts/compare_clock_skew_geo_no_agent.py
```

It fails closed unless both 60-second windows retain the primary retry/queue
failure, their request/timeout/rejection counters differ by no more than 15%,
running image IDs match, critical Service endpoint addresses stay fixed within
each arm, ready endpoint counts match across arms,
Geo's direct status changes 200→502, both deterministic primary mitigation
oracle checks pass, and the Geo treatment is removed cleanly. Treat one
passing pair as feasibility evidence, not a statistical estimate.

Only after those gates pass should the user run a matched control and
treatment agent pair. The commands use the same app image; only `--noise` and
the new profile differ:

```bash
.venv/bin/python main.py --force-build \
  --problem search_rate_retry_collapse_hotel_reservation \
  --stages diagnosis mitigation \
  --agent codex --model gpt-6-sol --judge-model gpt-4o-mini \
  --profile full --agent-timeout 1800 --n-attempts 1

.venv/bin/python main.py --force-build \
  --problem search_rate_retry_collapse_hotel_reservation \
  --stages diagnosis mitigation \
  --agent codex --model gpt-6-sol --judge-model gpt-4o-mini \
  --profile full --agent-timeout 1800 --n-attempts 1 \
  --noise --noise-profile clock-skew-geo --noise-duration-seconds 7200
```

The user runs paid agent and judge attempts; Codex may run no-agent smoke
checks. Do not infer a distraction rate from one matched pair.
