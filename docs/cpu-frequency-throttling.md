# CPU frequency noise with the Hotel Reservation selector problem

This profile is an implementation candidate for a bare-metal CloudLab cluster. Its [case design](agents/cpu-frequency-selector-design.md) and the [noise gate](agents/noise-case-design-requirements.md) define the required runtime checks. Do not count an agent attempt unless the no-agent smoke and matched control pass on the same hardware.

The primary problem is `wrong_service_selector_hotel_reservation`: the `frontend` Service has no endpoints because its selector contains `current_service_name=frontend`, a label the frontend Pod does not have. The noise lowers the CPU frequency ceiling of an isolated worker running the real `recommendation` Pod. A direct business RPC client keeps recommendation traffic alive even while the broken frontend Service blocks user traffic. The Linux [CPUFreq policy interface](https://docs.kernel.org/admin-guide/pm/cpufreq.html#policy-interface-in-sysfs) applies a `scaling_max_freq` limit to all CPUs in each policy on that worker; it is distinct from a container CPU quota.

## Preconditions

- A ready SREGym full-profile CloudLab bare-metal cluster with a dedicated worker for recommendation and at least one other schedulable node for the protected Hotel Reservation services and workload. The current single control-plane node has no scheduling taint, so one additional worker is the minimum for that cluster. The target worker must be available for recommendation alone among Hotel Reservation Deployments; only explicitly allowlisted node DaemonSets are accepted. Shared control-plane components or unrelated application Pods on that worker fail the isolation gate.
- Populate `worker_nodes.hosts` in `scripts/ansible/inventory.yml` with the current CloudLab allocation. It currently contains no workers. The target Kubernetes node name must map to exactly one worker SSH host. The operator's SSH key must already be trusted in `known_hosts`, and `sudo -n` must work on that host.
- Every CPUFreq `policy*` directory on the worker has writable `scaling_max_freq`, a supported lower cap, and readable policy state. The node-exporter must expose `node_cpu_scaling_frequency_hertz` to Prometheus. The full-profile Pushgateway and Prometheus must be reachable.
- The selector fault and the normal 100 request/s Hotel Reservation mixed workload must work without noise. The profile fails closed when any condition is missing. It does not run on Kind and does not substitute CFS CPU limits.

## No-agent smoke

Run from the repository root on the SREGym host after the full observation stack is ready. Replace `worker-rec` with the exact `kubectl get nodes` name of the dedicated worker. The script deploys Hotel Reservation, starts its default workload, injects the selector fault, calibrates frequency, runs the automatic gates, restores CPUFreq, recovers the selector, and removes the application namespace. It does not call an agent or judge model.

```bash
kubectl get nodes -o wide
kubectl get pods -A -o wide --field-selector spec.nodeName=worker-rec
uv run python scripts/smoke_cpu_frequency_noise.py --worker-node worker-rec
```

An unsuccessful calibration is an invalid configuration, not a passed smoke. Save the emitted evidence and check that every original CPUFreq policy maximum and governor is restored. The target's current frequency is measured through node-exporter under load; a written cap alone does not pass. The script runs control and treatment against the same deployment and checks their endpoint identities, fault oracle results, and `/hotels` success. It uses a frozen client deadline derived from the healthy p99, a 60-second control window with no more than 1% recommendation RPC timeouts, and a 60-second treatment window with at least 10%. It also temporarily repairs the selector in control and treatment preflights: `/hotels` must succeed at least 99% of the time and differ by no more than one percentage point between them. The faulty selector is reinstated before agent access.

## Matched agent commands

Run a no-frequency-change control on the same node layout first. The `--cpu-frequency-worker-node` option renders identical application placement in both arms: recommendation is required on that worker; every other Hotel Reservation Deployment and the `wrk2` workload Job are excluded. Both arms run the same direct 39 RPC/s client and publish the same metrics. Only the treatment command changes host frequency. Do not run the treatment agent command until the smoke passes.

```bash
uv run main.py \
  --problem wrong_service_selector_hotel_reservation \
  --stages diagnosis mitigation \
  --agent codex --model gpt-5 --judge-model gpt-5 \
  --profile full --cpu-frequency-worker-node worker-rec \
  --noise --noise-profile cpu-frequency-control \
  --agent-timeout 1200
```

```bash
uv run main.py \
  --problem wrong_service_selector_hotel_reservation \
  --stages diagnosis mitigation \
  --agent codex --model gpt-5 --judge-model gpt-5 \
  --profile full --cpu-frequency-worker-node worker-rec \
  --noise --noise-profile cpu-frequency-throttling \
  --agent-timeout 1200
```

## What an agent can observe

The profile exposes actual `GetRecommendations` client latency and timeout telemetry through the existing Prometheus stack. The same `RecommendationRpcTimeouts` alert rule exists in control and treatment; it fires only when real timeout measurements cross the threshold. Node frequency can be queried through node-exporter. The direct RPC client uses the real recommendation Service at 39 RPC/s with the workload's distance/rating/price mix; it runs until final cleanup and creates no Kubernetes observer Pod. Host-only evidence is saved with the attempt results.

The selector fault remains diagnosable with ordinary commands:

```bash
kubectl get svc frontend -n hotel-reservation -o yaml
kubectl get endpoints frontend -n hotel-reservation
kubectl get pods -n hotel-reservation --show-labels
kubectl get pods -n hotel-reservation -l current_service_name=frontend
kubectl patch svc frontend -n hotel-reservation --type=json \
  -p='[{"op":"remove","path":"/spec/selector/current_service_name"}]'
```

The expected diagnosis is the mismatched Service selector and empty endpoints. Repairing it must restore `/hotels` while the separate recommendation timeout symptom may remain. The noise does not change the diagnosis or mitigation oracle.
