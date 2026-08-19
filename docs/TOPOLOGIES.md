# DGX Spark fabric topologies

## Decision

The platform supports four directly connected pairs and one switched eight-node
fabric. The switched fabric is the recommended target for this fleet because
capacity must move dynamically between inference and training and larger models
may need more than two Sparks. Direct pairs remain a valid lower-complexity or
rollback design.

```text
Four direct pairs                     One switched fabric

spark-01 ===== spark-02               spark-01 ----+
spark-03 ===== spark-04               spark-02 ----+
spark-05 ===== spark-06               spark-03 ----+
spark-07 ===== spark-08               spark-04 ----+-- 200 Gb/s switch
                                       spark-05 ----+
Four independent scheduling groups    spark-06 ----+
                                       spark-07 ----+
                                       spark-08 ----+
                                       One scheduling group
```

Each Spark has two external ConnectX-7 QSFP ports. One physical cable exposes two
Linux Ethernet interfaces because the NIC has two paths to the SoC. NVIDIA states
that one cable per direct or switched link is sufficient and that each port is
capped at 200 Gb/s. Interface names must be discovered on the actual systems;
the example inventories use the documented right-port names.

References:

- [NVIDIA ConnectX-7 networking](https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html)
- [NVIDIA two-Spark connection guide](https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/connect-two-sparks)
- [NVIDIA switched-cluster playbook](https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/multi-sparks-through-switch)
- [NVIDIA Sync Cluster Assistant](https://docs.nvidia.com/sync/latest/cluster-assistant.html)

## Side-by-side comparison

| Concern | Four direct pairs | One eight-node switch |
| --- | --- | --- |
| Natural distributed width | 2 Sparks | 2, 4, or potentially 8 Sparks |
| Placement | Fixed pair eligibility | Dynamic selection from all healthy nodes |
| Capacity fragmentation | Higher; a healthy partner can be stranded | Lower; the scheduler can form a new group |
| Larger models | Must fit on one pair | Can span a wider validated group |
| Concurrent small jobs | Strong isolation across four pairs | Flexible, but jobs compete within one pool |
| Failure domain | Usually one pair or cable | Shared switch or bridge may affect the whole fabric |
| Network operations | Four repeatable point-to-point configurations | Switch firmware, ports, VLAN/bridge, backups, and monitoring |
| Hardware | Direct cables | Compatible 200 Gb/s switch and one cable per Spark |
| Admission | Pair-aware placement | Mandatory all-or-nothing gang admission |
| Troubleshooting | Simple path and peer set | More paths, congestion, and shared state |
| Expansion | Add another isolated pair | Add ports/nodes within switch capacity |
| NVIDIA assistant | Supports two-device direct setup | Supports switched setup only through four devices |
| Eight-node confidence | Four independently testable pairs | Must be manually configured and fleet-tested |

## Four direct pairs

### Behavior

Each pair has two logical IP networks carried over its direct cable. Kubernetes
labels both nodes with the same pair-specific `fabric-group`. A tensor-parallel
model anchored in `pair-b`, for example, may use only `spark-03` and `spark-04`.

Normal placement maps pairs A–C to inference preference and pair D to training
preference. The labels may be changed, but physical pair membership cannot.

### Advantages

- Minimal network equipment and configuration.
- Predictable latency and bandwidth between two known peers.
- A bad cable or pair configuration has a small blast radius.
- Four pairs can run independent jobs without sharing a switch.
- Troubleshooting and rollback are straightforward.

### Disadvantages

- A distributed job cannot naturally grow beyond two Sparks.
- One failed or occupied member can strand its partner.
- Free GPUs in other pairs cannot satisfy a two-node placement without moving the
  whole workload to another complete pair.
- The 6/2 policy is more closely tied to physical pair placement.

### Acceptance tests

- Confirm one approved 200 Gb/s cable per pair.
- Verify both logical interfaces on both members have carrier and unique addresses.
- Ping both logical peer addresses and run point-to-point bandwidth tests.
- Run NCCL collectives and a two-node vLLM request on every pair.
- Disconnect each cable and prove only that pair's distributed workload fails.

## One switched fabric

### Behavior

One cable from each Spark connects the same physical QSFP port to a compatible
200 Gb/s switch. Both logical interfaces receive addresses in two fabric-wide
subnets. Switch ports share the intended layer-2 segment, and every node receives
the common `fabric-a` scheduling label.

Kubernetes can then select any two, four, or eight eligible nodes, but the
physical network does not pool memory automatically. vLLM, Ray, PyTorch, or
another runtime must explicitly distribute each workload.

### Advantages

- Capacity moves freely between the normal 6/2 policy and temporary modes.
- A healthy node is not stranded when a former partner fails.
- Wider models and training jobs become possible after validation.
- Teams can change model placement without recabling or selecting a pair.
- The topology better matches dynamic cluster scheduling.

### Disadvantages

- The switch adds cost, firmware, configuration, telemetry, and backup duties.
- A switch outage or bad common configuration can affect all distributed jobs.
- Congestion and oversubscription must be measured rather than assumed away.
- Gang scheduling and queue policy become essential.
- NVIDIA Sync does not configure eight-device topologies; Ansible and the network
  team own the repeatable configuration.
- Successful two- or four-node NCCL tests do not prove efficient eight-node scaling.

### Acceptance tests

- Confirm exact switch, cables, firmware, port mode, and support status.
- Verify every participating port negotiates 200 Gb/s after reboot.
- Prove management traffic survives a fabric outage.
- Run an all-to-all address, bandwidth, latency, and packet-loss matrix.
- Run NCCL `all_reduce` and `all_gather` at widths 2, 4, and 8.
- Baseline vLLM at widths 1, 2, and 4 before allowing wider serving.
- Test Kueue admission under the six-inference/two-training guarantee.
- Test a failed node, cable, switch port, and switch reboot during workloads.
- Back up and restore both switch configuration and Spark netplan.

NVIDIA's Cluster Assistant supports at most four devices and only configures
networking; it does not install Kubernetes or workloads. Eight-node results are
therefore acceptance evidence for this fleet, not an implied vendor guarantee.

## Shared network rules

Management and fabric traffic stay separate:

| Network | Traffic |
| --- | --- |
| Enterprise management | SSH, Ansible, K3s API, etcd, Argo CD, APISIX, image pulls |
| ConnectX-7 fabric | NCCL, Ray collectives, tensor parallelism, FSDP |

Do not add a default route or DNS to the fabric netplan. Pin distributed runtimes
to the validated fabric interfaces and keep the management path available for
recovery. Jumbo frames are enabled only when the switch and every endpoint have
been tested with the same MTU.

## Changing topology

Changing from pairs to switched is a maintenance event, not an ordinary model
rollout:

1. Drain distributed workloads and preserve training checkpoints.
2. Back up current netplan and switch configuration.
3. Recable and configure the switch through the network change process.
4. Apply the switched private inventory with `playbooks/fabric.yml`.
5. Complete all fabric acceptance tests.
6. Change the Argo CD source to the switched cluster profile.
7. Admit a two-node canary, then four-node work, before enabling wider jobs.

Rollback reverses the sequence using the direct-pair inventory and known cable map.
