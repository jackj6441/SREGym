# Hotel Reservation topology reference

This note captures the topology of the Hotel Reservation application as it is
currently deployed by SREGym. It is a design input for noise selection, not a
substitute for a case-specific isolation test.

## Sources consulted

- [DeathStarBench Hotel Reservation README](https://github.com/delimitrou/DeathStarBench/blob/master/hotelReservation/README.md): the application is a Go and gRPC hotel-reservation service with search, recommendation, and reservation operations.
- [DeathStarBench paper](https://doi.org/10.1145/3297858.3304013): the benchmark suite models end-to-end, loosely coupled microservice applications.
- The current SREGym application definition: [`sregym/service/apps/hotel_reservation.py`](../../sregym/service/apps/hotel_reservation.py), metadata in [`sregym/service/metadata/hotel-reservation.json`](../../sregym/service/metadata/hotel-reservation.json), and the deployed chart in [`SREGym-applications/hotelReservation/helm-chart/hotelreservation`](../../SREGym-applications/hotelReservation/helm-chart/hotelreservation).
- Current call sites in [`services/frontend/server.go`](../../SREGym-applications/hotelReservation/services/frontend/server.go) and [`services/search/server.go`](../../SREGym-applications/hotelReservation/services/search/server.go), plus the workload mix in [`mixed-workload_type_1.lua`](../../SREGym-applications/hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua).

The OpenTelemetry Demo architecture linked in the research discussion is a good
example of documenting a service graph. It is **not** the topology of this
application, so it must not be copied into Hotel Reservation analysis.

## Service graph

```text
workload / client
        |
        v
    frontend (HTTP entrypoint)
      |       |          |          |           \
      v       v          v          v            v
   search   profile  recommendation user     reservation
      |                  |                     |
      +--> geo           +--> mongodb-          +--> mongodb-reservation
      +--> rate                 recommendation  +--> memcached-reserve
             |
             +--> mongodb-rate + memcached-rate

profile     --> mongodb-profile + memcached-profile
geo         --> mongodb-geo
user        --> mongodb-user
```

All application services use Consul for service registration/discovery, and
Jaeger is an observability component. These shared components are not safe
default noise targets: an issue there can propagate across transactions.

## Transaction paths exercised by the default workload

The default workload is not a single request path. Its approximate distribution
is read directly from `mixed-workload_type_1.lua`:

| Request | Share | Application path |
| --- | ---: | --- |
| `GET /hotels` | 60% | frontend -> search -> geo + rate; frontend -> reservation (availability) -> profile |
| `GET /recommendations` | 39% | frontend -> recommendation -> mongodb-recommendation; frontend -> profile |
| `POST /user` | 0.5% | frontend -> user -> mongodb-user |
| `POST /reservation` | 0.5% | frontend -> user + reservation -> reservation storage/cache |

Consequences for noise selection:

- `geo`, `rate`, `search`, `profile`, and reservation dependencies are all on
  the dominant search path.
- `recommendation` and its MongoDB are on a high-volume path, despite being
  semantically separate from hotel search.
- `user` and reservation are low-volume in the default mix, but are still
  critical to login and booking; they are candidates only after a case-specific
  argument and non-interference validation.
- Shared services such as `frontend`, Consul, or node-level infrastructure are
  not isolated candidates for a frontend-service fault.

## Implication for the current primary fault

For `wrong_service_selector_hotel_reservation`, the primary fault removes
`frontend` endpoints. Its direct critical boundary is therefore the client to
`frontend` Service path; all client transactions fail before reaching backend
services. A backend service may be a candidate for a secondary fault, but it is
not automatically safe merely because it is "downstream of frontend." The
noise design must show that its failure has no extra effect on the main probe
and does not alter the primary fault's endpoint, traffic, or oracle evidence.

For another primary fault, redraw the critical boundary from that fault's
specific transaction(s); do not reuse this conclusion blindly.

## Timestamp-aware search-rate experiment variant

The `search_rate_retry_collapse_hotel_reservation` experiment uses a custom
Hotel Reservation image for both its no-noise and clock-skew arms. In that
image, one in ten `/hotels` requests starts a bounded, asynchronous
`frontend -> recommendation` lookup. This is an optional side branch, not a
dependency of the hotel-search response. The protected transaction still
requires `frontend -> search -> geo/rate`, then reservation availability and
profile. The clock-skew treatment targets only the existing recommendation
Pod. See [`clock-skew-search-rate-design.md`](clock-skew-search-rate-design.md)
for the non-interference argument and runtime validation gates.
