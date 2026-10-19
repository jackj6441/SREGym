# Astronomy Shop checkout alert experiment

The primary problem is the existing `incorrect_port_assignment`. It changes
checkout's Product Catalog address from port 8080 to 8082. A failed
`POST /api/checkout` should fire the endpoint-specific `HighRequestErrorRate`.
The optional `astronomy-ad-image` profile makes a new `ad` Pod fail to pull its
image, causing `PodStatusError` while the existing Ready ad replica continues
serving. The [design note](agents/astronomy-checkout-ad-image-noise-design.md)
records the topology evidence and required non-interference gates.

Run comparable mitigation-only attempts from the repository root with the
full deployment profile:

```bash
.venv/bin/python main.py --problem incorrect_port_assignment --stages mitigation --profile full --agent stratus
.venv/bin/python main.py --problem incorrect_port_assignment --stages mitigation --profile full --agent stratus --noise --noise-profile astronomy-ad-image
```

The noise profile fails before the agent starts unless the checkout endpoint
alert is firing, checkout requests fail, cart requests succeed, ad has traffic,
and the ad rollout failure produces its own firing alert without changing the
primary-path endpoints or outcomes. Host-only evidence is saved with benchmark
results. The mitigation agent sees only the checkout endpoint alert and must
investigate its cause. The Stratus weak oracle checks only that alert instance;
the problem's own mitigation oracle checks checkout's actual configured
Product Catalog connection. The ad image is restored during final cleanup.

For manual inspection during a run:

```bash
kubectl -n astronomy-shop get deploy checkout ad
kubectl -n astronomy-shop get pods -o wide
kubectl -n astronomy-shop get endpoints frontend-proxy frontend checkout cart product-catalog ad
kubectl get --raw '/api/v1/namespaces/observe/services/prometheus-server:80/proxy/api/v1/alerts'
```

The six-run GPT-5 CloudLab result is documented in
[the experiment report](experiment-reports/astronomy-checkout-alert-gpt5-2026-10-03.md).
