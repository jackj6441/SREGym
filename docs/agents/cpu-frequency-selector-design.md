# CPU frequency noise beside the Hotel Reservation selector fault

Status: implementation candidate. No agent experiment is valid until the runtime gates below pass on a bare-metal CloudLab cluster.

## Primary fault and topology

The existing `wrong_service_selector_hotel_reservation` problem adds `current_service_name=frontend` to the `frontend` Service selector. No frontend Pod has that label, so the Service has no ready endpoints even while its Pod is Running. Every external transaction entering through that Service fails before reaching any backend. The protected recovery transaction is `GET /hotels`: frontend -> search -> geo and rate -> rate's Memcached (and MongoDB on a cache miss), followed by frontend -> reservation (availability) -> profile and their storage. The mitigation is to remove the incorrect selector and restore endpoints.

Topology evidence combines the [upstream Hotel Reservation README](https://github.com/delimitrou/DeathStarBench/blob/master/hotelReservation/README.md), the [local topology reference](hotel-reservation-topology.md), the deployed `SREGym-applications/hotelReservation/kubernetes` manifests, the `frontend`, `search`, and `rate` Go call sites, and `mixed-workload_type_1.lua`. The script issues approximately 60% `/hotels`, 39% `/recommendations`, and 0.5% each login and reservation at the normal 100 request/s setting. Existing selector-fault traces under `.scratch/cloudlab-ms1008-results` are supporting evidence, not the topology source of truth.

## Secondary fault and isolation

`recommendation` calculates hotel suggestions by distance, rating, or price from its own MongoDB-backed startup data. It serves `GET /recommendations` through frontend, but `/hotels` does not call it for its required response. Rate's MongoDB and Memcached, Geo, Search, Reservation, and Profile are on the protected search path and must not be targeted.

The treatment lowers `scaling_max_freq` on a separate physical worker hosting exactly the recommendation application Pod among Hotel Reservation workloads. The protected path Pods and their dependencies, shared control-plane components, discovery, ingress, storage, and workload generators must be on other nodes. The placement override excludes the `wrk2` workload Job from that worker as well as the other Hotel Reservation Deployments. Only named node DaemonSets for Kubernetes networking, node-exporter, Chaos Mesh's inactive daemon, and OpenEBS disk discovery/export remain eligible; the OpenEBS local PV provisioner and application storage Pods are excluded. The target node must be a non-control-plane worker with an exact inventory-to-Kubernetes identity match. A colocated protected component or uncertain mapping fails setup. The treatment does not change Kubernetes Service selectors, Pod labels, image, CPU quota, or governor.

The broken frontend Service prevents its normal 39% recommendation traffic from reaching the backend. A host-side client therefore sends genuine `recommendation.Recommendation/GetRecommendations` requests directly to the existing recommendation Service at a fixed 39 RPC/s in both control and treatment arms, before and after selector repair. It uses the same distance/rating/price mix as the Lua workload. No extra observer Pod is created. The client publishes its real response latency and timeout counts through the existing full-profile Prometheus stack with neutral metric names; the same alert rule is installed in both arms. No synthetic failures or agent-facing noise labels are added.

## Fault mechanism and lifecycle

The injector reads every CPUFreq policy's original governor, minimum and maximum frequency, hardware bounds, and current frequency. It only writes a lower `scaling_max_freq`, leaving governor and minimum unchanged. A bounded calibration tries supported lower caps, restoring the original policy between trials, and chooses the least severe cap that meets the effect and isolation gates. Every write is read back; a policy or node that cannot be controlled fails setup. The injector never falls back to a cgroup CPU limit. It targets no control-plane or Kind node.

The primary selector fault is established before calibration. The selected cap remains active through diagnosis, mitigation, and oracle validation. The manager does not follow a recommendation Pod that an agent moves to another node; that move is recorded as an agent intervention. Final cleanup or abort restores every original policy value and verifies it. Failed restoration invalidates the attempt and blocks a subsequent one.

## Automatic gates before an agent run

1. Confirm a healthy bare-metal CloudLab cluster, full Prometheus deployment, an exact target worker identity, exactly one Ready recommendation Pod, and physical placement isolation. Reject Kind, unsupported CPUFreq, missing frequency telemetry, or any protected workload on the target worker.
2. Confirm normal deployment and the selector fault: `frontend` has the injected selector and zero ready endpoints; a `/hotels` request through the Service fails; the existing mitigation oracle reports failure. Record the protected Service endpoint identities.
3. Under the unchanged 100 request/s mixed workload and direct 39 RPC/s client, measure a no-noise 60-second baseline. Set and freeze a client timeout above the healthy p99 with a margin chosen before treatment. Require recommendation timeouts at most 1%. Repeat the healthy recommendation check after selector repair in a matched control arm because frontend traffic then resumes.
4. During calibration, require the CPUFreq policy cap to read back lower than baseline, a real measured frequency reduction under load, and agent-queryable node frequency telemetry. Over a 60-second window require at least 10% direct recommendation RPC timeouts. A cap that does not meet this effect fails setup; do not shorten the already-frozen client deadline.
5. Require the primary Service endpoints, Pod placement, selector, and oracle outcome to remain unchanged by the frequency treatment. After selector repair, require `/hotels` success at least 99% and no more than one percentage point below the matched no-noise arm in a 60-second window. The recommendation failure may remain. The treatment must stay in force through this oracle validation.
6. Record host-only, allowlisted evidence of node and Pod identity, per-policy original/applied/restored values, actual frequency samples, client timeout and latency windows, `/hotels` results, endpoint identities, oracle results, and cleanup status. Do not expose treatment labels as diagnosis hints.
7. Restore every modified policy on normal completion, timeout, setup failure, and abort; verify restoration and remove the direct RPC client and its pushed metrics. If target placement changes, record the agent intervention and mark subsequent treatment measurements separately instead of retargeting another node.

Passing unit tests or a single control/treatment pair establishes implementation feasibility, not a valid agent result. A no-agent CloudLab smoke test and matched control must pass before formal attempts. The primary outcome of later agent experiments is wrong attribution of the hotel outage or failure to repair the selector; extra investigation time and actions are secondary.

See [noise-case-design-requirements.md](noise-case-design-requirements.md) for the repository-wide gate.
