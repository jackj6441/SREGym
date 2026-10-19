# CloudLab 上跑 SREGym：中文实验说明与命令手册

> 更新时间：2026-09-18。本文只记录已经验证过的事实，以及明确列出的后续计划；不会把「能启动」误写成「实验成功」。API key 永远不写进本文、Git 或 shell history。

## 一句话说明

现在我们已经把 SREGym、Kubernetes、Muse Spark 1.3 和 judge 放到了 CloudLab。单机已经可以完整启动一次 SREGym。接下来要先让 Muse 在**没有 noise**时稳定提交诊断/修复答案，再分别验证时钟偏移、CPU 抢占、CPU 降频三种干扰，最后才做可比较的三次重复评估。

## 现在有什么、已经做到哪里了

| 项目 | 大白话状态 | 证据/位置 |
| --- | --- | --- |
| CloudLab 节点 | 已可 SSH、可 `sudo`。当前是 `ms1040.utah.cloudlab.us`，16 CPU、约 62 GiB 内存。 | `ssh jackj@ms1040.utah.cloudlab.us` |
| Kubernetes | 已部署，当前是单节点 control plane。 | 节点上的 `kubectl get nodes` |
| SREGym 环境 | 已 clone 到 `~/SREGym`，`uv sync`、Helm、OpenCode 都已安装。 | `~/SREGym` |
| 模型连通性 | Muse Spark 1.3 原生 preflight 和 `gpt-4o-mini` judge preflight 都通过。 | 下方“预检查”命令 |
| selector-mismatch case | 已实现并可被选择：`wrong_service_selector_hotel_reservation`。故障是 Service selector 不再匹配 Pod label，Service 没有 endpoints。 | 当前分支 `jack/muse-clock-skew` |
| clock-skew noise | 已实现一个 `clock-skew` profile：为独立 observer Pod 创建 Chaos Mesh `TimeChaos`，固定 `+5m`。 | 当前分支 `jack/muse-clock-skew`、`docs/clock-skew-noise.md` |
| CPU noisy-neighbor noise | 已在独立分支 `jack/cpu-noisy-neighbor` 实现，但**尚未整合到当前 clock-skew 分支**。它会让业务 Pod 与一个无 CPU limit 的 `analytics-backfill` 压力 Pod 在同一 worker 抢 CPU。 | commit `899fa05b` |
| CPU-frequency-throttling | 尚未实现。 | 是下一项开发工作，不要假装已有可运行 CLI。 |

### 已跑过的一次无-noise 基线，结果应如何理解

2026-09-18 在 CloudLab 上跑过：

```text
problem: wrong_service_selector_hotel_reservation
agent:   opencode/muse-spark-1.3-contributor-free
judge:   gpt-4o-mini
profile: full
noise:   none
```

它完成了部署、注入、agent 运行和清理；结果在：

```text
~/SREGym/results/0918_1150/opencode/wrong_service_selector_hotel_reservation/run_1/
```

但 CSV 记录为 `agent_return_code=0`、`incomplete_stage=diagnosis`、`phase.stage:diagnosis.outcome=no_submission`。意思是：Muse 正常退出了，但没有把 diagnosis 提交给 SREGym。因此这次是“基础设施冒烟通过、benchmark 结果未通过”，下一步优先排查这个不提交行为。

## 三种 noise 到底分别模拟什么

| noise | 模拟的现实情况 | 放在哪里 | 成功的最低证据 |
| --- | --- | --- | --- |
| `clock-skew` | 某个进程/容器看到的时间比正常时间快 5 分钟。它不是 CPU 干扰。 | 单节点也可以；目标是独立 observer Pod，不碰宿主机时钟。 | `TimeChaos` 已注入，并且 observer 内**持续运行的进程**显示的时间真的与正常时间差约 5 分钟；清理后恢复。 |
| `cpu-noisy-neighbor` | 同机的别的工作负载吃掉 CPU，使业务变慢或出现 CPU contention。 | 独立 worker；业务 Pod 和压力 Pod 必须在同一 worker。control plane 不参与抢 CPU。 | 压力 Pod 存在、`kubectl top pod`/Prometheus 显示 CPU 上升、删除 noise 后下降。 |
| `cpu-frequency-throttling` | 机器 CPU 本身被限频，业务即使没被别的 Pod 抢 CPU，也会变慢。 | 独立 worker；只改 worker，不改 control plane。 | 注入前后能读到实际频率/ governor 的变化；恢复脚本把原设置还原。 |

### 重要的技术诚实说明

目前 `clock-skew` observer 的 shell loop 会反复启动新的 `date` 子进程；Chaos Mesh 是否会让这些**注入之后才启动**的子进程继承时间偏移，尚未在 x86 CloudLab 验证。不能只看到 `TimeChaos=AllInjected` 就宣称它有效。先做下面的可观测性验证；若时间没有变化，需要把 observer 改为一个长期运行、直接读取时钟的进程后再评估。

## 目标拓扑：为什么 CPU 需要两台机器

```text
CloudLab control plane                         CloudLab worker
ms1040 (当前机器)                         新增的一台 x86_64 节点
  Kubernetes API / scheduler                    Hotel Reservation Pods
  SREGym runner、Muse、judge                     CPU noise Pod 或限频动作
  不承受 CPU noise                              同一 worker 上产生和观察效果
```

当前单节点适合：无-noise 基线、Muse 排障、clock-skew 验证。

当前单节点不适合：两种 CPU noise 的正式结论。因为压力或限频也会影响 Kubernetes API/scheduler，无法区分“业务被干扰”还是“控制面被拖慢”。CPU 实验开始前，在 CloudLab portal 增加一台与当前节点同类的 Ubuntu x86_64 节点；保留当前节点为 control plane。

## 统一的正式比较口径

四组唯一变化的是 noise，其余参数完全固定：

```text
case:          wrong_service_selector_hotel_reservation
agent:         opencode/muse-spark-1.3-contributor-free
judge:         gpt-4o-mini
deployment:    full
agent timeout: 1200 seconds
```

执行顺序：

1. 无-noise：先跑 1 次，直到 Muse 能提交 diagnosis 和 mitigation；随后跑 3 次。
2. `clock-skew`：先验证 noise 本身 1 次，随后与无-noise 同口径跑 3 次。
3. `cpu-noisy-neighbor`：在两节点拓扑上验证 1 次，随后跑 3 次。
4. `cpu-frequency-throttling`：完成实现和恢复机制后，在两节点拓扑上验证 1 次，随后跑 3 次。

每次都保留 `results/`、agent trajectory、SREGym log、Pod placement 和必要的 Prometheus 指标。失败时保留现场和日志，不自动 reimage。

## 现在就能在当前 CloudLab 节点执行的命令

以下命令都在 CloudLab 节点上运行，不是在 Mac 上运行。

### 1. 登录并做只读健康检查

在 Mac terminal：

```bash
ssh jackj@ms1040.utah.cloudlab.us
```

登录后：

```bash
cd ~/SREGym
kubectl get nodes -o wide
kubectl get pods -A
df -h /
docker info >/dev/null && echo 'Docker OK'
```

### 2. 预检查模型，不暴露凭据

凭据文件已经应当只存在于节点的 `~/.config/sregym/env`，权限应为 `600`。先确认权限，不打印文件内容：

```bash
stat -c '%a %n' ~/.config/sregym/env
```

然后运行预检查：

```bash
source ~/.config/sregym/env
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH"
cd ~/SREGym

JUDGE_MODEL_ID='gpt-4o-mini' \
uv run python -c 'from main import run_judge_preflight_check; run_judge_preflight_check()'

AGENT_MODEL_ID='opencode/muse-spark-1.3-contributor-free' \
uv run python -c 'from clients.opencode.driver import run_preflight; run_preflight()'
```

### 3. 重新跑一次无-noise 基线，并实时看输出

先在节点启动一个 tmux session：

```bash
tmux new -s sregym-baseline
```

在 tmux 里面粘贴：

```bash
source ~/.config/sregym/env
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH"
cd ~/SREGym

uv run main.py \
  --problem wrong_service_selector_hotel_reservation \
  --stages diagnosis mitigation \
  --agent opencode \
  --model opencode/muse-spark-1.3-contributor-free \
  --judge-model gpt-4o-mini \
  --profile full \
  --agent-timeout 1200
```

用 `Ctrl-b` 再按 `d` 可退出 tmux 但不中断实验。之后重新连接：

```bash
tmux attach -t sregym-baseline
```

实验结束后，立刻看最新结果的关键字段：

```bash
cd ~/SREGym
latest_csv="$(find results -name '*_opencode_results.csv' -type f | sort | tail -n 1)"
echo "$latest_csv"
python3 - "$latest_csv" <<'PY'
import csv
import sys

with open(sys.argv[1], newline='') as f:
    row = next(csv.DictReader(f))
for key in sorted(row):
    if any(word in key.lower() for word in ('return_code', 'incomplete', 'stage', 'outcome', 'judge', 'problem')):
        print(f'{key}={row[key]}')
PY
```

只有在 diagnosis 和 mitigation 都出现有效 submission、并通过对应 oracle 后，才能把它标为“成功基线”。

### 4. 当前可运行的 clock-skew 冒烟验证

这条命令只用于验证 clock-skew 的注入和可观测性，不能在验证前称为正式比较结果：

```bash
source ~/.config/sregym/env
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH"
cd ~/SREGym

uv run main.py \
  --problem wrong_service_selector_hotel_reservation \
  --stages diagnosis mitigation \
  --agent opencode \
  --model opencode/muse-spark-1.3-contributor-free \
  --judge-model gpt-4o-mini \
  --profile full \
  --agent-timeout 1200 \
  --noise \
  --noise-profile clock-skew \
  --noise-duration-seconds 120
```

在另一 terminal 观察注入对象（实验运行期间）：

```bash
kubectl get timechaos -A
kubectl get pods -A -l sregym.io/noise-profile=clock-skew -o wide
kubectl logs -n hotel-reservation -l sregym.io/noise-profile=clock-skew --tail=50
```

验证点是 observer log 的时间确实偏移，而不只是 CRD 存在。

## 两节点 CPU 实验的准备计划

### A. 先从 portal 增加 worker

在 CloudLab portal 创建/修改实验时，目标为两个物理节点：当前 `ms1040` 继续承担 control plane；新增节点作为 worker。两个节点都应是 Ubuntu x86_64，并能用同一个 `jackj` SSH key 登录和 `sudo`。

新增节点 ready 后，先在 Mac 上确认，不要立刻在运行中的 cluster 上反复执行完整安装：

```bash
ssh jackj@<new-worker-hostname> 'hostname; nproc; free -h; uname -r; sudo whoami'
```

然后把 `scripts/ansible/inventory.yml` 的 `worker_nodes.hosts` 补为新节点，先做无副作用连通性检查：

```bash
cd /Users/jackjiang/Desktop/Research/SREGym
uv run ansible-inventory -i scripts/ansible/inventory.yml --graph
uv run ansible -i scripts/ansible/inventory.yml all -m ping
```

`setup_cluster.yml` 当前同时包含 control-plane 初始化和 worker join。对已经在使用的 control plane，先检查其幂等性/worker-join 路径，再运行；不要盲目把完整 playbook 当成“只加一个 worker”的命令。worker 加入完成的验收是：

```bash
ssh jackj@ms1040.utah.cloudlab.us 'kubectl get nodes -o wide'
```

输出中必须有一个 Ready control-plane 节点和一个 Ready worker 节点。随后确认 Hotel Reservation workload 落在 worker：

```bash
kubectl get pods -n hotel-reservation -o wide
```

### B. 把 CPU noisy-neighbor 正确整合进当前研究线

现在 clock-skew 和 CPU noisy-neighbor 分属两个分支，不能直接假定二者已经同时存在。下一项代码工作是把 `899fa05b` 的小型 noise profile 和测试移植/合并到 `jack/muse-clock-skew`，解决冲突后运行相应测试。完成前，下面的 CPU 命令只是目标命令，**不能执行**：

```bash
# 仅在 cpu-noisy-neighbor 已整合进当前分支后执行
uv run main.py \
  --problem wrong_service_selector_hotel_reservation \
  --stages diagnosis mitigation \
  --agent opencode \
  --model opencode/muse-spark-1.3-contributor-free \
  --judge-model gpt-4o-mini \
  --profile full \
  --agent-timeout 1200 \
  --noise \
  --noise-profile cpu-noisy-neighbor \
  --noise-cpu-workers 2 \
  --noise-duration-seconds 120
```

CPU noise 的冒烟验收：压力 Pod 与 Hotel Reservation Pod 同在 worker、压力 Pod CPU 明显升高、噪声清理后 Pod 和 CPU 使用都消失/下降。

### C. 实现 CPU-frequency-throttling 后再写最终命令

这项功能还不存在。实现时必须先决定并测试：

1. 只允许改 worker 的频率，拒绝改 control plane。
2. 记录每个 CPU 原始 governor/频率设置。
3. 注入后读回实际频率，而非只相信命令返回码。
4. 无论 agent 成功、失败或超时，cleanup 都恢复原设置。
5. 不支持可控 cpufreq 的 CloudLab 节点应明确 skip/fail，不伪造结果。

实现和测试通过后，把它作为一个明确的 `--noise-profile` 加入 CLI，再把最终命令补到这里；在此之前不要编造 profile 名或参数。

## 三次正式评估如何跑

每个 profile 在 smoke test 合格后，连续跑三次。以无-noise 为例：

```bash
source ~/.config/sregym/env
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH"
cd ~/SREGym

for run in 1 2 3; do
  echo "===== baseline run ${run}/3: $(date -Is) ====="
  uv run main.py \
    --problem wrong_service_selector_hotel_reservation \
    --stages diagnosis mitigation \
    --agent opencode \
    --model opencode/muse-spark-1.3-contributor-free \
    --judge-model gpt-4o-mini \
    --profile full \
    --agent-timeout 1200
done
```

clock-skew 的三次运行在上面每次命令末尾附加：

```text
--noise --noise-profile clock-skew --noise-duration-seconds 120
```

两种 CPU profile 也采用同样方式，只能在 worker topology 和 noise 自身 smoke test 都通过之后运行。

## 每次实验后要带走什么

在节点上，将某一次结果目录压缩到自己的 home（不要包含 `~/.config/sregym/env`）：

```bash
cd ~/SREGym
tar -C results -czf "$HOME/sregym-results-$(date +%Y%m%d-%H%M%S).tgz" <结果目录名>
ls -lh "$HOME"/sregym-results-*.tgz | tail
```

在 Mac 下载：

```bash
scp jackj@ms1040.utah.cloudlab.us:'~/sregym-results-*.tgz' ./
```

保存结果时至少包含：SREGym CSV、每次 `run_*/trajectory.json`、`driver.log`、agent 输出和与 noise 对应的 Kubernetes/Prometheus 观测记录。

## 下一步待办清单

- [ ] 复跑无-noise 基线，定位 Muse `no_submission` 的原因，直到 diagnosis 与 mitigation 可提交。
- [ ] 在 x86 CloudLab 验证 clock observer 的时间真的偏移；若否，修复 observer 的长期进程设计。
- [ ] 在 CloudLab 增加第二台 x86_64 worker，并验收其成功加入 Kubernetes。
- [ ] 将 `cpu-noisy-neighbor` 的小型实现合并到 clock-skew 研究分支并跑单元测试。
- [ ] 实现带可靠恢复的 `cpu-frequency-throttling` profile，并做硬件能力检测。
- [ ] 每种 profile 先 1 次 smoke test，再与无-noise 组各 3 次正式评估。
- [ ] 比较四组的提交率、oracle 结果、完成时间、轨迹/trace 和 noise 指标；只报告实际采集到的数据。
