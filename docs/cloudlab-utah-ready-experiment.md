# Accessing a ready CloudLab Utah experiment

This note records the supported way to reach nodes in an existing, **Ready**
CloudLab experiment. It intentionally does not assume a generic Utah SSH
gateway or guess a node hostname.

## Access workflow

1. Open the experiment's CloudLab status page. `Ready` means its startup
   scripts have completed; `Booted` only means provisioning is finished.
   [CloudLab Kubernetes tutorial](https://docs.cloudlab.us/cloudlab-manual.html#exploring-your-experiment)
2. Use **Topology View** for an in-browser shell, or **List View** to find
   every assigned node and CloudLab's complete SSH command. **Manifest View**
   has the allocated-resource details. Copy the current List View command
   rather than constructing a hostname or SSH user yourself.
   [CloudLab Getting Started](https://docs.cloudlab.us/getting-started.html)
3. Run that command from a workstation holding the private half of the public
   key registered for the experiment. The experiment owner has `sudo`/root
   access on its nodes.
   [CloudLab Getting Started](https://docs.cloudlab.us/getting-started.html)

## SSH key behavior

CloudLab supports public-key authentication for direct experiment-node SSH.
Upload the public key through **username → Manage SSH keys** in the portal;
CloudLab installs it on SSH-capable nodes at the **next experiment
instantiation**. Consequently, adding a key now is not documented as a way to
gain direct SSH access to an already-running experiment: use a key that was
already installed, the browser shell, or ask the experiment owner to arrange
access/reinstantiate.

Sources: [SSH setup](https://docs.cloudlab.us/cloudlab-manual.html#setting-up-ssh-access),
[List View authentication](https://docs.cloudlab.us/cloudlab-manual.html#list-view).

## Utah hostnames and network boundary

For Utah, the CloudLab documentation gives both permanent physical node FQDNs
such as `amd047.utah.cloudlab.us` and per-experiment virtual FQDNs such as
`node1.myexpt.myproj.utah.cloudlab.us`. Those FQDNs resolve to the public
control interface, which is the interface used for SSH; do not use it for
experiment traffic. The experiment network instead uses private `10.x.x.x`
addresses and short names from `/etc/hosts`.

The Utah aggregate controller is `boss.utah.cloudlab.us`, but the official
documentation identifies it as an aggregate/API controller—not a general SSH
bastion. There is no documented universal Utah login hostname for experiment
nodes. The experiment's List View remains the authoritative source of the
actual SSH command.

Sources: [CloudLab control network](https://docs.cloudlab.us/control-net.html),
[Utah aggregate definition](https://docs.cloudlab.us/geni-lib/_modules/geni/aggregate/cloudlab.html),
[Utah hardware and control network](https://docs.cloudlab.us/hardware.html#cloudlab-utah).

## VM caveat

Physical nodes normally expose a routable control address. VMs normally do
not; CloudLab supplies basic VM SSH through a non-standard port on the
physical host. Discover that port in the Manifest or List View rather than
assuming port 22.

Source: [CloudLab virtual machines](https://docs.cloudlab.us/virtual-machines-advanced.html).
