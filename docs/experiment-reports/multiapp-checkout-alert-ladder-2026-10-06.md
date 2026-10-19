# 五应用、多故障告警实验（2026-10-06）

## 问题与设置

每轮都在同一个 CloudLab Kubernetes 集群部署 Astronomy Shop、Hotel Reservation、Social Network、Fleet Cast 和 Blueprint Hotel Reservation。固定主故障为 Astronomy Shop checkout 的 `PRODUCT_CATALOG_ADDR=product-catalog:8082`；正常地址为 `product-catalog:8080`。负载中的 `POST /api/checkout` 因此失败，`GET/POST /api/cart` 是健康对照。给 GPT-5 Stratus agent 的任务只指出 Astronomy Shop 中 firing 的 `HighRequestErrorRate`（`POST /api/checkout`），不告知故障、根因或其他故障的数量。

五个条件按 1→5 个总故障递增，每个条件独立重建五个应用，预检通过后只运行一次 agent。实际交给 agent 的最后一句任务是：

> Astronomy Shop has a firing HighRequestErrorRate alert for POST /api/checkout. Investigate and resolve this alert.

附加故障与所需 firing 告警按固定顺序累加：

| 故障序号 | 应用与注入 | 所需 firing 告警 |
| --- | --- | --- |
| 1 | Astronomy checkout 调用 Product Catalog 的端口改成 8082 | `HighRequestErrorRate{namespace="astronomy-shop",method="POST",name="/api/checkout"}` |
| 2 | Astronomy ad 镜像拉取失败，新 Pod 无法启动，旧端点保留 | `PodStatusError`（新 ad Pod） |
| 3 | Hotel `recommendation` Service selector 错配，Ready Pod 存在但 Service 无端点 | `ServiceEndpointDown` |
| 4 | Social `user-service` Deployment 缩容到 0 | `DeploymentNotReady` |
| 5 | Hotel `mongodb-rate` Pod 的内存 request 超过节点容量 | `PodSchedulingFailure` |

每一步的主故障预检都要求目标告警 firing、checkout 失败率至少 90%、两个 cart 接口健康、主路径 Service endpoints 不变、请求率保持门槛、主故障 oracle 仍失败。附加故障还必须同时有真实 Kubernetes 症状和自己的 firing 告警。注入或预检失败的尝试不计为 agent 失败。方法和门槛见 [设计记录](../agents/multiapp-checkout-alert-ladder-design.md)。

## 正式运行结果

| 总故障数 | 预检 | 目标告警修复 | TTM（秒） | Agent 行为与备注 |
| ---: | --- | --- | ---: | --- |
| 1 | 通过 | 是 | 413.1 | 查阅 trace、metrics、Pod、Service、endpoints 和 checkout Deployment 后，将地址改回 8080；20 轮上限自动提交。 |
| 2 | 通过 | 是 | 332.0 | 发现 checkout Deployment 配置错误并改回 8080；检查 rollout、trace 与目标告警后主动提交；ad 告警保留。 |
| 3 | 通过 | 是 | 350.3 | 查 checkout trace、Deployment 和日志后改回 8080；20 轮上限自动提交；Hotel selector 故障未修。 |
| 4 | 通过 | 是 | 382.6 | 改回 8080，但还给 Product Catalog Service 增加 `8082 -> 8080` 端口；此额外修改非必要，20 轮上限自动提交。 |
| 5 | 通过 | 是 | 345.3 | 查 checkout trace、Service 与 Deployment，改回 8080；确认 rollout、成功的 checkout trace 和目标告警消失后主动提交；未修改其他应用或故障。 |

五轮正式运行的目标告警全部恢复，结果 CSV 的 `fault_preflight_evidence.steps` 记录了每个注入步骤。注入后的 checkout 失败率约为 100%（Prometheus 独立 `rate` 估计有时略高于 1），两个 cart 接口的失败率均为 0。预检记录中的 checkout 请求率仅约 0.019–0.066 rps，因此短期速率会明显波动。第 5 轮在最后一个 `PodSchedulingFailure` firing 后仍有约 0.019 rps 的 checkout 请求，失败率为 100%，两个 cart 接口各约 0.048 rps 且无失败。第 4 轮的多余 Service 端口修改表明只用目标告警是否恢复作为 success 指标，不能代表 agent 的所有改动都合适；第 1、2、3、5 轮已证明只恢复 checkout 地址即可修复目标。

第 5 轮第一次部署后的预检被正确拒绝：ad 注入后两分钟 checkout 请求率短暂降至 0.0095 rps，低于 0.01 rps 门槛；checkout 仍约 100% 失败、cart 健康。agent 未启动。预检随后改为在 90 秒内取得间隔五秒的两次完整通过采样；没有放宽原门槛。清理后重跑，五个故障及告警均通过预检，agent 运行有效。

## 判读边界

- 每个条件只有一次正式 agent 运行，不能据此估计成功率或故障数导致的趋势。
- Blueprint Hotel Reservation 在此版本已部署，但其 workload 方法是 no-op；该应用没有新增请求负载。
- 第 2 轮曾出现与任务无关的 TiDB 告警；相关误报规则在第 3 轮前修正。该轮虽符合预设目标和噪声预检，观察到的环境告警并非完全受控。
- 早期部署、容量或预检失败的尝试均排除在正式结果外。集群节点的根分区扩至约 216 GiB、kubelet `maxPods` 调至 200；这是让五应用能稳定共存的宿主机准备工作。

## 原始记录

五份正式结果 CSV 也已复制到仓库本地的 `results/multiapp-checkout-alert-ladder-2026-10-06/`（该目录按现有 `.gitignore` 不纳入版本控制）。CloudLab 主机：`jackj@ms1008.utah.cloudlab.us`。测试目录：`/users/jackj/SREGym-checkout-alert-test-20261003`。正式运行的 CSV、`trajectory.json` 和 `phases_attempt1.jsonl` 分别位于：

- `results/1005_2147/stratus/multiapp_checkout_alert_1/`
- `results/1005_2226/stratus/multiapp_checkout_alert_2/`
- `results/1006_1117/stratus/multiapp_checkout_alert_3/`
- `results/1006_1147/stratus/multiapp_checkout_alert_4/`
- `results/1006_1329/stratus/multiapp_checkout_alert_5/`

第 5 轮首次、未进入 agent 的预检记录位于 `results/1006_1306/stratus/multiapp_checkout_alert_5/phases_attempt1.jsonl`，其中 `inject_fault` 阶段记录了上述 0.0095 rps 门槛失败。正式重跑结束后，五个应用命名空间均已删除，节点仍为 Ready，根分区剩余约 163 GiB。

运行命令形式：

```bash
SREGYM_AGENT_IMAGE=sregym-agent-base:latest \
  .venv/bin/python main.py --problem multiapp_checkout_alert_N \
  --stages mitigation --profile full --agent stratus --model gpt-5 \
  --agent-timeout 1200
```
