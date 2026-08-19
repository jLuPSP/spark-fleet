# Ansible deployment guide

## What Ansible owns

Ansible manages DGX host prerequisites, persistent ConnectX-7 addressing, K3s,
the NVIDIA container runtime, the GPU Operator, KubeRay, node labels, and GPU
taints. It does not own model deployments, APISIX application configuration, or
team policy after Argo CD is enabled.

It can also perform an operator-invoked model pre-stage using the same verified
artifact format as the GitOps staging Jobs. This is useful before a maintenance
window or large activation; Argo remains the deployment owner.

## Prepare a private inventory

Choose one topology and copy its placeholder inventory:

```bash
cp inventory/dgx-spark-pairs.example.yml inventory/dgx-spark-pairs.private.yml
# or
cp inventory/dgx-spark-switched.example.yml inventory/dgx-spark-switched.private.yml
```

Private inventories are ignored. Replace `PLATFORM_ADMIN` and each management
address, then verify the ConnectX-7 interface names on every Spark:

```bash
ibdev2netdev
ip -br link
```

The examples use `enp1s0f1np1` and `enP2p1s0f1np1`, the two Linux interfaces
documented for the right QSFP port. Hardware/OS discovery is authoritative.
The corresponding RoCE devices are commonly `rocep1s0f1` and `roceP2p1s0f1`;
the cluster profiles pass both to NCCL so one physical link can use both PCIe paths.

Corporate environments may define `enterprise_proxy_env` for bootstrap downloads
and `k3s_registry_config` for private image mirrors in the private inventory. Put
proxy passwords and registry credentials in Ansible Vault or the enterprise
secret manager, never in an example inventory.

For example, a private `k3s_registry_config` may map public names to a transparent
corporate mirror while keeping the digest-pinned image contract:

```yaml
k3s_registry_config:
  mirrors:
    docker.io:
      endpoint: [https://CONTAINER_REGISTRY]
    nvcr.io:
      endpoint: [https://CONTAINER_REGISTRY]
```

The defaults intentionally use `default({})` inside the playbook, so private
inventory and Vault values are not shadowed by a higher-precedence vars file.

Before editing a cluster profile, record the installed NVMe SKU and free space:

```bash
ansible spark_nodes -i inventory/dgx-spark-switched.private.yml \
  -b -a 'lsblk -b -o NAME,SIZE,MODEL,MOUNTPOINTS'
ansible spark_nodes -i inventory/dgx-spark-switched.private.yml \
  -b -a 'df -BG /var/lib/spark-fleet/models'
```

Set `cluster.model_storage.capacity_gb` to the observed 1000 or 4000 GB class.
Do not activate a model while it remains null.

## Inventory model

Both examples define the same functional groups:

| Group | Meaning |
| --- | --- |
| `k3s_server` | Initial schedulable K3s server |
| `k3s_agents` | Seven schedulable agents |
| `spark_nodes` | All nodes receiving ConnectX configuration |
| `fabric_pair_*` | Direct peers; present only in the pair inventory |

Each host declares two `fabric_interfaces` because one Spark QSFP link exposes two
Linux Ethernet interfaces. `fabric_validation_group` controls the ping matrix:
one pair for direct cabling, all eight nodes for the switched design.

The examples use private placeholder subnets. Replace them through the enterprise
IP allocation process if those ranges conflict with existing routes.

## Preview and apply fabric configuration

Fabric changes default to preview-only:

```yaml
fabric_apply: false
```

Run discovery and syntax checks first:

```bash
ansible-inventory -i inventory/dgx-spark-switched.private.yml --graph
ansible-playbook -i inventory/dgx-spark-switched.private.yml playbooks/fabric.yml --syntax-check
ansible-playbook -i inventory/dgx-spark-switched.private.yml playbooks/fabric.yml
```

After reviewing the reported interfaces and `/etc/netplan/40-spark-fabric.yaml`
template, set `fabric_apply: true` in the private inventory. The playbook then:

1. verifies both declared interfaces exist;
2. backs up and writes the dedicated netplan file;
3. applies nodes serially;
4. requires carrier on both logical interfaces;
5. pings every peer in the topology on both interface planes.

Use the management network for Ansible. The fabric file does not define a default
route, so an interconnect error should not remove management access.

## Bootstrap the complete cluster

The site playbook applies fabric configuration first, followed by host and K3s
configuration:

```bash
ansible-playbook \
  -i inventory/dgx-spark-switched.private.yml \
  playbooks/dgx-spark.yml
```

Run it a second time as the idempotency check. Expected changes on the second run
are limited to explicit verification or components whose upstream installer
reports harmless drift.

For four pairs, substitute `dgx-spark-pairs.private.yml`. The playbook code is
the same; the inventory supplies topology-specific addresses, validation groups,
and Kubernetes labels.

## Pre-stage active models

Render the topology-specific host matrix, then run the staging playbook:

```bash
make stage-vars-switched
ansible-playbook \
  -i inventory/dgx-spark-switched.private.yml \
  playbooks/stage-models.yml \
  -e model_stage_manifest_file=build/model-stage-switched.yml
```

Supply `model_store_bearer_token` and an optional `model_store_ca_bundle` through
Ansible Vault or the enterprise secret manager. The playbook never contacts
Hugging Face; it downloads only checksummed artifacts from the internal store.

## GPU posture

DGX OS owns the GPU driver. Ansible verifies it rather than replacing it. The
NVIDIA Container Toolkit is pinned for K3s, while GPU Operator is installed with
driver and toolkit management disabled. GPU Operator still supplies discovery,
the device plugin, and monitoring components.

Every Spark is tainted `nvidia.com/gpu=present:NoSchedule`. Rendered inference and
training workloads carry the matching toleration and an exclusive GPU claim.
Time-slicing and consumer-GPU assumptions are intentionally absent.

## Labels produced by the inventories

Direct pairs use:

```text
spark-fleet.example/fabric-topology=direct-pairs
spark-fleet.example/fabric-group=pair-a
spark-fleet.example/preferred-workload=inference
```

The switched design uses:

```text
spark-fleet.example/fabric-topology=switched
spark-fleet.example/fabric-group=fabric-a
spark-fleet.example/preferred-workload=inference
```

Sparks 7 and 8 receive `preferred-workload=training` in both examples. This is a
normal-mode preference, not dedicated ownership.

## Rollback

Before changing netplan, the template task creates a timestamped backup. To roll
back a fabric change, use the management connection to restore the known-good
file and run `netplan apply`. Do not switch from the switched inventory to the
pair inventory until cabling matches the target topology.

K3s recovery uses the pinned installer and an external datastore snapshot. Model
and gateway rollback is performed through Git/Argo CD, not this playbook.
