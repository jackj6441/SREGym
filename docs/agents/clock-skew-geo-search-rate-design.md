# Geo clock skew beside search-rate retry collapse

Status: proposed. Do not run an agent experiment until the no-agent gates below
and a matched control pass. The existing `clock-skew` recommendation profile and
its `20260924.1` image remain unchanged; this case uses `clock-skew-geo` and a
new, separately tagged application image.

## Primary fault and topology

The primary problem is `search_rate_retry_collapse_hotel_reservation`. The
protected request is `GET /hotels`. Frontend calls Search; Search calls Geo
`Nearby` before Rate; after Search succeeds, Frontend calls Reservation for
availability and Profile for hotel details. A ten-second, 40 req/s burst fills
Rate's bounded 256-entry queue. Search's 750 ms Rate RPC deadline and up to
three attempts sustain excess Rate work after load returns to 8 req/s. The
post-trigger symptom is failed hotel searches; the expected diagnosis is the
Search/Rate timeout–retry–queue feedback loop.

Topology sources: the [upstream Hotel Reservation README](https://github.com/delimitrou/DeathStarBench/blob/master/hotelReservation/README.md)
describes nearby-hotel search and reservation as application operations;
the current [topology reference](hotel-reservation-topology.md), deployed
`SREGym-applications/hotelReservation/kubernetes` manifests, and the local
Frontend, Search, and Geo Go call sites define this checkout's actual graph.
The no-agent 60-second post-trigger CloudLab probe on 2026-09-27 submitted
480 `/hotels` requests, observed 0% success, about 1,443 Search→Rate attempts,
and a Rate queue around 255–256. Search calls Geo before each Rate request,
so Geo was reached by those searches; this is source-and-counter inference,
not a direct Geo RPC measurement. Jaeger returned no traces despite known
traffic and is not evidence of zero Geo calls.

## Candidate semantics and isolation risk

Geo uses its in-memory spatial index (loaded from MongoDB) to return nearby
hotel IDs. The protected `/hotels` path invokes its `Nearby` RPC; the new
read-only `GET /nearby-hotels?lat=...&lon=...` frontend path will invoke the
same business RPC and return nearby hotel IDs. It makes no reservation,
profile write, or other persistent change. A bounded asynchronous lookup from
every tenth `/hotels` request will invoke the same read-only RPC, log an
optional failure, and never alter the protected request's response.

**Geo is a sibling of Rate below Search, not downstream of Rate and not outside
the protected transaction.** This is an explicit exception to the preferred
service-level placement in [the noise-case standard](noise-case-design-requirements.md).
The only defensible isolation is at the *consumer behavior* boundary: Search
uses Geo's hotel IDs and ignores the new generation-time gRPC header; the new
frontend browse/prefetch consumer validates the header and rejects a result
more than one minute ahead of frontend time. This is not proof that shifting
Geo's process clock is otherwise harmless. If the runtime non-interference
checks show any extra `/hotels` effect, the Geo design fails and must not be
used for an agent run.

## Fault mechanism and lifecycle

Geo stamps each `Nearby` result with its own wall-clock generation time in
gRPC response metadata. The new frontend consumer accepts synchronized
timestamps and returns HTTP 200. A single Chaos Mesh TimeChaos object shifts
only the existing, Ready Geo application's container clock by +5 minutes,
selected by exact Pod name. The frontend's direct browse request then returns
HTTP 502 with a future-timestamp error; sampled `/hotels` requests generate a
bounded warning without changing their response. Search continues to use the
same Geo hotel IDs. No benchmark-only Pod is created, and Geo must stay
Running/Ready. The treatment is injected once after the primary fault is
established, remains through diagnosis and both oracle stages, and is removed
before the application is torn down. Cleanup must verify normal browse
responses if the target Pod still exists. Pod replacement by an agent is
recorded as a separate intervention, not silently retargeted.

## Automatic gates and experiment plan

1. Require all eight Go Deployments in each arm to use one immutable image
   reference. Use the same new Geo image for both the no-noise and treatment
   arms. Keep the historical Recommend image and `clock-skew` profile runnable.
2. Require exactly one Ready Geo Pod and a healthy direct browse response
   before injection. Require active primary failure before treatment: backed-up
   Rate queue and low `/hotels` success.
3. After injection, require the same Geo Pod UID to remain Running/Ready,
   direct browse to fail specifically because its timestamp is ahead of
   frontend time, and a sampled optional-lookup warning in frontend logs.
4. Require unchanged Frontend, Search, Geo, and Rate Service endpoints; a
   still-backed-up Rate queue; and the same low-success class of protected
   `/hotels` requests. A large success-rate change fails setup.
5. Compare a matched, no-agent 60-second post-trigger window in both arms:
   `/hotels` success, Search→Rate attempts, Rate receives/timeouts/rejections,
   Rate queue, and primary fault oracle result. Also compare a post-mitigation
   protected request and oracle result. No agent run if the treatment causes
   an additional primary-path failure or changes the oracle outcome.
6. Save only allowlisted host-side evidence: image reference, direct HTTP
   status before/after, primary metrics, target Pod UID, TimeChaos name,
   cleanup status, and whether the target identity changed. Do not expose
   benchmark labels or setup hints to the agent. Delete TimeChaos and verify
   browse recovery (or target replacement), then remove the application.

Passing preflight proves feasibility for that deployment, not statistical
non-interference. A matched control/treatment pair is a pilot; repeat before
drawing conclusions about agent distraction. The user runs paid agent/judge
attempts; no-agent smoke is allowed as a separate infrastructure check.

## 2026-09-27 no-agent pilot

Using the same temporary CloudLab image in both arms, the first 60-second
post-trigger pair showed:

| Metric | Control | `clock-skew-geo` |
| --- | ---: | ---: |
| `/hotels` submitted / completed / succeeded | 480 / 480 / 0 | 480 / 480 / 0 |
| Search→Rate attempts | 1,441 | 1,438 |
| Rate requests received | 1,440 | 1,440 |
| Rate requests expired | 1,202 | 1,202 |
| Rate requests rejected | 239 | 238 |
| Rate queue, start → end | 256 → 255 | 256 → 256 |

Treatment preflight observed direct browse HTTP 200→502, Geo Pod UID
unchanged, queue 254→256, and `/hotels` success 0% before and after injection.
The treatment cleanup was confirmed; afterward the application namespace and
TimeChaos list were empty. This pair supports traffic non-interference in the
tested failure state only.

The subsequent automated paired check, using the same image and independent
60-second windows, printed `PAIR_PASS`. Control versus treatment:
`/hotels` success was 0/480 in both; Search→Rate attempts were 1,441 versus
1,439; Rate receives 1,440 versus 1,439; expired requests 1,202 in both;
and queue depth remained at 253–256. Geo's direct route changed 200→502 with
the future-timestamp error. After applying the documented safe Search/Rate
policy, the deterministic primary mitigation oracle passed in **both** arms:
normal `/hotels` traffic recovered to 100% and stayed healthy after replaying
the 40 req/s trigger. Geo treatment cleanup was confirmed, the target UID did
not change, and the application namespace and TimeChaos list were empty.
This is one feasibility pair, not proof of general causal non-interference.

See [the noise-case standard](noise-case-design-requirements.md) and the
operator [Geo runbook](../clock-skew-geo-noise.md).
