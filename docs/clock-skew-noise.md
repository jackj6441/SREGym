# Clock-skew noise profile

The `clock-skew` profile creates one non-critical
`analytics-time-indexer` Pod on the same worker as the problem's frontend
Pod. It is a real but isolated fault: Chaos Mesh applies a `+5m`
`CLOCK_REALTIME` offset to only the Pod's `indexer` container.

The Pod has two containers that share a small local volume:

- Both containers run the `analytics-time-indexer` native binary from a
  digest-pinned multiarch image. `reference-clock` writes its unmodified epoch time;
  `indexer`, which starts before TimeChaos is applied, repeatedly calls
  `clock_gettime(CLOCK_REALTIME)` in the same long-lived process.
- After three consecutive samples are within five seconds of the configured
  positive five-minute offset, `indexer` records `CLOCK_SKEW_FAULT` in its
  logs and fails its readiness probe.

The observer intentionally does not fork `date` or another short-lived child
process to measure time: on some Chaos Mesh/container-runtime combinations,
processes created after injection do not inherit the time shift. The long-lived
native process is what makes this fault measurable on CloudLab.

Therefore the observable noise symptom is a Running but `NotReady` observer
Pod (normally `1/2 Ready`) with a measured clock delta. It is intentionally
different from the benchmark's primary Service-selector symptom: it does not
change application Pods, Services, EndpointSlices, application traffic, the
Kubernetes control plane, or node clocks.

Placement prefers a non-control-plane worker. On a single-node cluster where
the Ready frontend Pod necessarily runs on the control-plane node, the observer
falls back to that same node; the time shift remains scoped to the observer
container.

The manager first waits for the two-container Pod to be healthy, applies
TimeChaos, then requires both the `NotReady` symptom and the clock-delta log
marker before the agent can start. If the cluster does not actually apply the
time offset, setup fails rather than running a case with fake or silent noise.
It attempts cleanup; if Chaos Mesh resource deletion cannot be confirmed, that
cleanup failure is surfaced rather than deleting the indexer first.

## Blind evaluation contract

The indexer remains visible to the agent as a normal, secondary analytics
workload. Its labels, containers, and image name contain no `sregym` or
`noise` identifier, so agent-visible metadata does not reveal that it is a
benchmark treatment. The agent can still discover the genuine clock symptom by
investigating the unhealthy workload and its logs; it is not instructed which
observed fault is causal.

Before an agent starts, the framework internally verifies the primary fault
and (when enabled) the clock-skew treatment. These checks are recorded only in
the result artifacts. A failed check is infrastructure-incomplete and never
counts as a model sample.

## Run

```bash
uv run main.py \
  --problem wrong_service_selector_hotel_reservation \
  --stages diagnosis mitigation \
  --agent opencode \
  --model opencode/muse-spark-1.3-contributor-free \
  --judge-model gpt-4o-mini \
  --noise \
  --noise-profile clock-skew \
  --noise-duration-seconds 3600
```

The selected profile is injected synchronously before diagnosis and remains
active through diagnosis, mitigation, and mitigation verification. It is
removed only during final attempt cleanup. Unlike random noise, deterministic
clock skew has no cooldown-based reinjection loop: the observer Pod and
TimeChaos resource keep the same identities for the whole attempt. A failed
profile setup fails the run rather than silently continuing without noise.

Use a duration long enough to cover every requested agent stage and oracle
evaluation. This duration is the Chaos Mesh treatment TTL, not a manager
reinjection cadence. The default is one hour; a shorter explicit value can make
the treatment expire before mitigation finishes.

## Verify and debug

```bash
kubectl get pods -n hotel-reservation -l app.kubernetes.io/name=analytics-time-indexer -o wide
kubectl get timechaos -n chaos-mesh
kubectl describe timechaos -n chaos-mesh <timechaos-name>
kubectl get pod -n hotel-reservation <analytics-time-indexer-pod>
kubectl logs -n hotel-reservation <analytics-time-indexer-pod> -c indexer
kubectl get pods -n chaos-mesh
```

Expected evidence after setup is a Running indexer Pod with `READY` equal to
`1/2`, plus a log line like `CLOCK_SKEW_FAULT offset_seconds=300`. The
`reference-clock` container stays Ready; only `indexer` is selected by TimeChaos.
Do not treat this isolated noise workload as the root cause or modify it while
mitigating the benchmark fault.

The TimeChaos resource selects the indexer by a unique opaque instance
label. Cleanup deletes the TimeChaos resource before deleting the indexer
Pod; Chaos Mesh restores the target clock when its resource is deleted.
