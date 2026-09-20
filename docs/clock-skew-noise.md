# Clock-skew noise profile

The `clock-skew` profile creates one `analytics-clock-observer` Pod on the
same worker as the problem's frontend Pod. Chaos Mesh then applies a `+5m`
`CLOCK_REALTIME` offset to only the observer container. The Pod is a
non-critical, SREGym-owned workload; it does not alter application Pods, the
Kubernetes control plane, or node clocks.

The observer writes its UTC time every five seconds. TimeChaos affects the
observer's PID 1 and its child processes, so inspect the container logs rather
than using `kubectl exec date` to see the offset.

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
kubectl get pods -n hotel-reservation -l sregym.io/noise-profile=clock-skew -o wide
kubectl get timechaos -n chaos-mesh
kubectl describe timechaos -n chaos-mesh <timechaos-name>
kubectl logs -n hotel-reservation <analytics-clock-observer-pod>
kubectl get pods -n chaos-mesh
```

The TimeChaos resource selects the observer by a unique `sregym.io/noise-run`
label. Cleanup deletes the TimeChaos resource before deleting the observer
Pod; Chaos Mesh restores the target clock when its resource is deleted.
