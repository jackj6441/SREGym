# Clock skew beside search-rate retry collapse

Status: implementation candidate. Agent runs are not valid until the runtime
preflight and matched no-noise baseline described below pass.

## Primary fault and topology

The primary problem is `search_rate_retry_collapse_hotel_reservation`. Its
protected client transaction is `GET /hotels`: frontend -> search -> geo and
rate, followed by frontend -> reservation (availability) -> profile. The rate
service has a bounded 256-entry queue and a 20 QPS backend policy; a temporary
40 req/s trigger causes timeouts and search retries that keep the queue
saturated after external traffic returns to 8 req/s. Its user-visible symptom
is failing hotel searches. The diagnosis must identify the persistent
timeout/retry/queue feedback loop, not merely a low static QPS setting.

Sources: the [upstream Hotel Reservation description](https://github.com/delimitrou/DeathStarBench/blob/master/hotelReservation/README.md)
and [DeathStarBench paper](https://doi.org/10.1145/3297858.3304013), the
[local topology reference](hotel-reservation-topology.md), the deployed
`SREGym-applications/hotelReservation/kubernetes` manifests, and
`SearchRateRetryCollapse` / `HotelSearchWorkload` in this checkout. The
completed no-noise `0924_1147` Sol trace showed a full rate queue and
retry-amplified search calls; it is a historical reference, not a matched
control because it predates the optional recommendation branch.

## Secondary fault and isolation

`recommendation` serves hotel suggestions based on distance, rating, or price.
It is not used to produce the required `/hotels` response. In the updated app,
one in ten `/hotels` requests also starts a bounded, non-blocking optional
recommendation lookup. This side branch runs in both experimental arms. It does
not participate in the search, rate, reservation, or profile calls that form
the primary transaction's critical path. The existing `/recommendations`
endpoint remains the direct user-facing path to this service.

The recommendation gRPC result carries a wall-clock generation timestamp. The
frontend rejects a result more than one minute in its future. Under normal
clocks, `/recommendations` succeeds. A +5 minute TimeChaos offset in only the
existing recommendation Pod makes its results future-dated: direct
`/recommendations` requests return an error, and sampled `/hotels` requests
leave a frontend warning while the original hotel-search response is unchanged.
The recommendation Pod stays Running and Ready. This is a faulty application
result, not a synthetic observer workload or a forced readiness failure.

TimeChaos selects the exact recommendation Pod by name and the application
container by name. It does not shift the node clock or any frontend, search,
rate, reservation, or profile container. The fault stays in place through
diagnosis, both oracle validations, mitigation, and final cleanup. Cleanup
deletes the TimeChaos resource; it does not delete the application's Pod.

## Gates before an agent run

1. Build the same timestamp-aware image for all eight Hotel Reservation Go
   services in both arms, so version differences are not an agent-visible clue;
   reject setup if its image reference is missing. The no-noise arm runs the
   same optional lookup with normal clocks.
2. Establish the primary post-trigger failure before injecting TimeChaos.
   Require the bounded rate queue to remain backed up and `/hotels` search
   success to remain low.
3. Require direct `/recommendations` to work before treatment. After
   treatment, require an HTTP error specifically caused by a future-dated
   recommendation result and a corresponding optional-lookup warning in
   frontend logs. If the Go process does not observe the +5 minute offset,
   fail setup rather than claiming a clock-skew treatment.
4. Require the primary frontend/search/rate Service endpoint identities to be
   unchanged, the queue to remain backed up, and the protected search workload
   to show the same failing class with no large success-rate change. Compare
   the matched no-noise and noise runs afterward as an additional check;
   runtime variation means a single pair is only a feasibility pilot.
5. Save a host-only, allowlisted JSON record of pre/post recommendation HTTP
   statuses, primary queue/success metrics, target Pod UID, TimeChaos name,
   target UID change, and cleanup outcome. A target changed by agent action is
   a secondary intervention outcome, not manager-driven treatment churn; the
   trajectory is needed to attribute the change.
6. Require final cleanup to remove TimeChaos and restore valid recommendation
   responses before another run. An aborted run is invalid if treatment state
   cannot be confirmed cleaned up.

Primary trace-analysis outcome: an agent incorrectly attributes the hotel-search
outage to the recommendation clock or fails safe primary mitigation because of
it. Merely mentioning or repairing the isolated recommendation error does not
count as primary diversion. Secondary outcomes: target replacement, extra
investigation steps, and time to diagnosis/mitigation. This analysis does not
change judge logic or retroactively regrade prior runs.

See [noise-case-design-requirements.md](noise-case-design-requirements.md) for
the repository-wide standard and [the runbook](../clock-skew-recommendation-noise.md)
for operator commands. The user, not Codex, runs the benchmark attempts.
