# Five-application checkout alert ladder

This is the design gate for the five-application, one-to-five-fault experiment.
It follows [noise-case-design-requirements.md](noise-case-design-requirements.md).
No agent run is valid until every runtime gate below passes for that run.

## Scope and topology

Deploy Astronomy Shop, Hotel Reservation, Social Network, Fleet Cast, and
Blueprint Hotel Reservation on the same cluster for each run. The primary
transaction is Astronomy Shop `POST /api/checkout`: load generator ->
frontend-proxy -> frontend -> checkout -> product-catalog. The existing
`incorrect_port_assignment` problem changes checkout's
`PRODUCT_CATALOG_ADDR` from `product-catalog:8080` to
`product-catalog:8082`. This breaks checkout while the `/api/cart` control
transactions remain healthy. The target alert is exactly
`HighRequestErrorRate{namespace="astronomy-shop",method="POST",name="/api/checkout"}`.

Topology sources are the OpenTelemetry Demo architecture and checkout source
recorded in [the Astronomy ad-image design](astronomy-checkout-ad-image-noise-design.md),
the application manifests under `SREGym-applications/opentelemetry-demo`, and
the deployed checkout problem and workload in this repository. The Hotel
Reservation upstream architecture, SREGym manifests, source call sites, and
workload mix are recorded in [the Hotel topology note](hotel-reservation-topology.md).
Social Network's `user-service` and Hotel's `recommendation` and
`mongodb-rate` are existing application services, not experiment-only pods.
Their concrete deployments are defined under `SREGym-applications/socialNetwork`
and `SREGym-applications/hotelReservation`. Fleet Cast and Blueprint Hotel
Reservation remain healthy background applications in this candidate ladder.
Runtime request rates, service endpoints, and alert labels must be captured
from the live five-application deployment; repository topology is not a
substitute for the runtime gate.

## Fixed fault order

Each run starts from a clean application state and keeps all earlier faults
active. One formal agent attempt is made per row.

| Total faults | Added fault | Real symptom | Required distinct firing alert |
| --- | --- | --- | --- |
| 1 | Astronomy checkout incorrect Product Catalog port | Checkout requests fail | Target `HighRequestErrorRate` |
| 2 | Astronomy `ad` failed image rollout | New ad Pod cannot pull image; old Ready Pod remains | `PodStatusError` for the new ad Pod |
| 3 | Hotel `recommendation` Service selector mismatch | Ready recommendation Pod exists but Service has zero endpoints | `ServiceEndpointDown` for `recommendation.hotel-reservation:8085` |
| 4 | Social `user-service` Deployment scaled to zero | No available user-service replica | `DeploymentNotReady` for `social-network/user-service` |
| 5 | Hotel `mongodb-rate` Deployment requests more memory than a node can provide | Replacement Pod is unschedulable | `PodSchedulingFailure` for its Pod |

The ad treatment uses the existing
`AstronomyAdImageTreatment`; the selector, replica, and resource request
faults reuse the existing SREGym problem injectors. Hotel recommendation
serves `/recommendations`; `mongodb-rate` backs the rate service used by
hotel search. Social `user-service` serves Social Network operations. These
services are in namespaces that the Astronomy checkout transaction cannot
call. The ad service is not on the checkout-to-product-catalog critical path;
its real traffic and endpoint preservation are established in the ad-image
design. Cluster-wide memory, CPU, scheduler, DNS, and observer contention can
still affect the primary transaction, so namespace separation alone is not
accepted as proof of non-interference.

## Automatic gates before agent launch

1. Confirm five applications and observer are Ready, existing app workloads
   are running where implemented, and node allocatable capacity has headroom.
   Keep workload settings fixed across all five runs. Blueprint's workload
   methods are currently no-ops, so it is deployed but does not add traffic.
   Require a clean baseline for the alert families used by this ladder; old
   Evicted Pods or unrelated endpoint/ingress alerts invalidate the condition.
2. Inject the primary fault and establish live `POST /api/checkout` traffic,
   at least 90% checkout failures, healthy `GET` and `POST /api/cart` controls,
   the exact firing target alert, and a failing primary mitigation oracle.
3. After *each* added fault, require its Kubernetes symptom and its own
   `firing` alert with namespace and workload labels. An injector call alone
   is never evidence of a fault.
4. After *each* added fault, compare Astronomy primary-path Service endpoints
   and checkout/cart request rates and failure ratios with the pre-noise
   snapshot. Keep the target alert firing and primary oracle failing. Abort
   before agent launch if an endpoint's 2-minute request rate drops below
   0.01 rps or 20% of its pre-noise rate, endpoint topology changes, the
   control cart requests fail, or the target alert disappears. After each
   additional fault, require two passing snapshots five seconds apart within
   90 seconds so a brief low-volume rate dip cannot decide the run.
5. Record each gate's snapshot and alert labels as host-only evidence. Give
   the agent only the target alert, Astronomy Shop context, and normal cluster
   observability. Do not disclose fault names, root causes, or which other
   alerts are deliberate.
6. Check the agent container's host-facing MCP routes for kubectl, Jaeger,
   and Prometheus immediately before launch. A broken port-forward is an
   infrastructure failure, not an agent outcome.

The formal success check is the existing checkout port mitigation oracle and
resolution of only the exact target alert. Other firing alerts are recorded
as distractors, not required repairs. Off-target edits by the agent are
reported separately. Injection, deployment, capacity, or alert-preflight
failure invalidates a condition and is not counted as an agent failure.

## Lifetime and cleanup

Keep all injected faults active through investigation and grading. Recover in
reverse order: resource request, replica count, Service selector, ad image,
then checkout port. Clean up all five applications and verify namespaces,
replicas, selectors, image, and checkout address are restored before the next
run. If cleanup cannot be confirmed, stop the ladder; do not run another
condition on contaminated state. Preserve host-only preflight and cleanup
evidence with the run artifacts.
