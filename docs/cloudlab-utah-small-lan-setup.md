# SREGym on a ready CloudLab Utah `small-lan` experiment

This is a short operational note for an already-provisioned **bare-metal**
CloudLab experiment. It is not for EWS or a managed Kubernetes service.

## Access the allocated nodes

1. Open the experiment's status page in the CloudLab portal. Confirm the
   experiment is ready and note its expiry; CloudLab reclaims nodes and their
   local disks at expiry unless the experiment is extended.
2. Open **List View**. It is the authoritative source of the allocated node
   names and displays the complete SSH command for every node. Copy that
   command rather than constructing a hostname or using an experiment-network
   address.
3. SSH to every allocated `small-lan` node and verify both privilege and
   connectivity:

   ```bash
   ssh <command-copied-from-list-view>
   sudo -v
   hostname -f
   ```

CloudLab's SSH access uses its public control network; do not use that network
as the benchmark data plane. Direct SSH requires a public key that was present
in the CloudLab account when this experiment was instantiated. If the ready
experiment lacks that key, use the portal's browser shell/serial console to
recover access or recreate it with the correct key; adding a key now is not
documented as retroactively installing it on existing nodes.

## Map the experiment to SREGym's remote-cluster inventory

SREGym's Ansible setup requires SSH and `sudo` on every host. Use the **exact
List View control-network hostnames** in the inventory.

The screenshot shows one allocated PC. That is enough for an initial
single-node control-plane cluster: the SREGym playbook removes the control-plane
`NoSchedule` taint, so ordinary workloads can run there. A single node is
appropriate for baseline application problems such as a Service-selector
mismatch, but it is not a faithful environment for worker/node fault or
noisy-neighbor evaluation. Provision at least one separate worker before those
experiments.

```bash
cd scripts/ansible
cp inventory.yml.example inventory.yml
# For one PC, edit inventory.yml to retain only control_node:
#   control_nodes.control_node.ansible_host: <control-node-from-list-view>
#   control_nodes.control_node.ansible_user: <your-CloudLab-login-user>
#   all.vars.k8s_user / k8s_user2: <your-CloudLab-login-user>
#   worker_nodes.hosts: {}
# For a multi-node experiment, add each worker's List View hostname under
# worker_nodes instead.

ansible-playbook -i inventory.yml setup_cluster.yml
kubectl get nodes -o wide
```

Keep `inventory.yml` local: it contains the SSH routing used by SREGym's
remote OS fault injector, which reads `worker_nodes` and runs its commands via
SSH with `sudo`. For node-level CPU experiments, use a separate worker and
confirm that the Kubernetes node names and inventory hostnames are recognisably
paired; that injector matches them by hostname and otherwise falls back to the
first worker.

For CPU-frequency experiments, run the capability check on a worker after
provisioning (for example, inspect `/sys/devices/system/cpu/cpufreq`) before
claiming a frequency fault was injected. Do not run benchmark workloads until
the cluster's nodes are `Ready`.

## Sources

- CloudLab, [Getting Started: use an experiment and SSH access](https://docs.cloudlab.us/getting-started.html)
- CloudLab, [Manual: experiment List View, public-key SSH, control vs. experiment network, and lifetime](https://docs.cloudlab.us/cloudlab-manual.html#sec:use-experiment)
- SREGym, [remote-cluster Ansible instructions](../scripts/ansible/README.md)
- SREGym, [inventory template](../scripts/ansible/inventory.yml.example) and [remote OS injector](../sregym/generators/fault/inject_remote_os.py)
