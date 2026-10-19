# Astronomy Shop checkout alert with an ad rollout failure

This case implements the requirements in [noise-case-design-requirements.md](noise-case-design-requirements.md).
The host runs the existing `incorrect_port_assignment` problem with optional
`--noise-profile astronomy-ad-image`. The agent is given the firing
`HighRequestErrorRate` for `POST /api/checkout`, with no fault configuration or
root-cause summary.

## Primary boundary and topology

The primary fault changes only checkout Deployment's `PRODUCT_CATALOG_ADDR`
from `product-catalog:8080` to `product-catalog:8082`. The affected Locust
transaction enters through `frontend-proxy`, reaches `frontend`, then invokes
`checkout`. Checkout reads the cart and calls `product-catalog` before it can
complete an order. On successful orders, later calls can reach currency,
shipping, payment, email, and Kafka; the Kafka consumers include accounting and
fraud-detection. The bad product-catalog address fails before those later steps.
`GET /api/cart` and `POST /api/cart` exercise a separate cart path and are
controls, not the expected primary failure.

Sources for this boundary:

- [Upstream architecture](https://opentelemetry.io/docs/demo/architecture/)
  describes the checkout and ad service relationships.
- The deployed [Helm values](../../SREGym-applications/astronomy-shop/charts/opentelemetry-demo/values.yaml)
  configure checkout's cart and product-catalog addresses, and separately
  configure frontend's `AD_ADDR` and `CHECKOUT_ADDR`. The existing
  [problem](../../sregym/conductor/problems/incorrect_port_assignment.py) and
  [mitigation oracle](../../sregym/conductor/oracles/incorrect_port.py) define
  the fault and its real dependency probe.
- CloudLab fault-only telemetry captured on 2026-10-03 (host-side snapshot
  `.scratch/astronomy-alert-noise/fault_only.json`):
  `POST /api/checkout` was 0.035 requests/s with 0.035 failures/s (five-minute
  averages), while `GET /api/cart` and `POST /api/cart` had zero failures.
  The checkout service's `HighRequestErrorRate` was firing. This runtime data
  supports, but does not replace, the call-path evidence above.

The target endpoint alert uses a two-minute rate window so it clears after
recent checkout requests recover. Its identity is
`{namespace="astronomy-shop", alertname="HighRequestErrorRate",
method="POST", name="/api/checkout"}`. The service-level checkout alert is
a separate instance and is not the success criterion. The primary mitigation
oracle still probes checkout's configured product-catalog address so a missing
alert caused by no traffic is not accepted as a repaired dependency.
The conductor's `/get_app` response passes only those target-alert labels to
the mitigation driver. The agent's artifact ID is deliberately anonymized, so
it is not used to identify the benchmark problem or reveal its fault.
The driver waits for this exact alert instance to enter `firing` before sending
the task, including in the no-noise control arm.
After mitigation, the targeted weak oracle allows up to four minutes for the
two-minute rate window and one-minute alert hold to clear, then requires two
minutes with that exact alert silent. It fails if the alert remains firing at
the deadline. The primary dependency oracle still checks the actual repair.

## Noise target and isolation

The `ad` service serves storefront advertisements by context key; see the
[upstream Ad service description](https://opentelemetry.io/docs/demo/services/ad/).
Frontend can call it while rendering storefront and product pages. It is not
on the `POST /api/checkout` transaction's checkout-to-product-catalog path.
The workload still reached `ad` with the primary fault active: server request
rate was 0.151/s in the healthy baseline and 0.130/s in the fault-only
five-minute window. Fraud-detection and accounting, initially considered as
noise targets, received 0 messages/s under the primary fault, so they were
rejected.

The profile patches only the `ad` Deployment's container image to a unique,
nonexistent release tag. The deployed `ad` Deployment has one replica and a
`RollingUpdate` strategy with `maxSurge=25%` and `maxUnavailable=25%`.
Kubernetes can retain the old Ready replica while the new revision's Pod waits
with `ErrImagePull` or `ImagePullBackOff`. The real observable symptom is a
failed `ad` rollout and a firing `PodStatusError` for that new Pod. The alert
groups waiting reasons by Pod because image pulls can switch between those two
reasons during retry; grouping by reason kept resetting its one-minute timer
in the first CloudLab smoke test. Other pod or Deployment alerts may cofire.
No benchmark-named resources or labels are created, and the treatment remains
until the agent and oracle finish.

This is an isolation hypothesis, not a traffic-volume argument: `ad` is on
storefront-rendering transactions but outside the checkout API's dependency
path. The host must reject a run if `ad` loses its original Ready Service
endpoint, if a primary-path endpoint changes, or if checkout/cart request
outcomes change relative to the fault-only state.

## Automatic gates and cleanup

The host profile must fail closed before opening the agent stage unless all
of the following pass:

1. Confirm the primary address is `product-catalog:8082`, the checkout
   dependency oracle fails, `POST /api/checkout` has recent requests and a
   high failure rate, cart controls succeed, `ad` has real server traffic,
   and its existing endpoint and Pod are Ready.
2. Record the Service endpoints on the primary path (`frontend-proxy`,
   `frontend`, `checkout`, `cart`, `product-catalog`) and for `ad`, plus the
   checkout and cart request rates and outcomes.
3. Patch only `ad`'s image. Wait until a new `ad` Pod has an image-pull waiting
   reason and its `PodStatusError` is firing. Confirm that the original `ad`
   endpoint remains Ready, primary-path endpoint sets are unchanged,
   checkout is still failing, cart controls still succeed, and the primary
   dependency oracle still fails. Require two consecutive five-second-apart
   snapshots in a 90-second window; checkout, cart, and ad request rates must
   each remain at least 0.01 rps and 20% of their fault-only rates. The lower
   proportional threshold accounts for variation in low-volume two-minute
   rate windows while still rejecting suppressed traffic.
4. During mitigation and oracle validation, retain the bad image. Before
   cleanup, verify the treatment still exists. Cleanup restores exactly the
   original image and replica count, waits for the rollout and Ready endpoint,
   and verifies no Pod still uses the bad image. On failed preflight, restore
   the original image before aborting.

A matched fault-only run and a fault-plus-noise run must pass these gates before
using agent outcomes for an experiment. Host-only preflight details must not be
sent to the mitigation agent.

## CloudLab smoke result (2026-10-03)

The first wrong-image attempt created an `ImagePullBackOff` Pod but did not
produce a firing `PodStatusError`: the original alert grouped by `reason`,
which alternated during image-pull retries. That attempt restored both the ad
image and the primary checkout address. After changing the alert grouping to
`(namespace, pod)`, a second fault-plus-noise attempt passed every pre-agent
gate:

| Measure, two-minute window | Fault only | Fault plus ad image failure |
| --- | ---: | ---: |
| `POST /api/checkout` requests/s | 0.0286 | 0.0276 |
| `POST /api/checkout` failure ratio | 0.917 | 1.001 (rate extrapolation) |
| `GET /api/cart` failure ratio | 0 | 0 |
| `POST /api/cart` failure ratio | 0 | 0 |
| `ad` server requests/s, five-minute window | 0.189 | 0.168 |

The six recorded Service endpoint sets were identical before and after noise.
The checkout dependency oracle failed on both sides of the injection.
`PodStatusError` fired for the new `ad` Pod, and the old Ready `ad` endpoint
remained in service. The bad image was still present 15 seconds after preflight
and cleanup restored the original image and one Ready replica. After primary
fault recovery, the dependency oracle passed and the specified
`POST /api/checkout` alert was no longer firing. Evidence is saved host-side
at `.scratch/astronomy-alert-noise/ad_image_smoke_evidence.json`.
