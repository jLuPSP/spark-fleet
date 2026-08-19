# Architecture

## Objective

Operate eight NVIDIA DGX Spark systems as a shared inference and training
platform without exposing Kubernetes, node addresses, or model-server credentials
to users. The public contract is one Entra-protected, OpenAI-compatible API.

The default capacity policy is 75% inference and 25% training. That policy is
logical: reviewed changes can temporarily assign all eight Sparks to inference or
four Sparks to a scheduled training burst.

## Stack and boundaries

| Layer | Choice | Why |
| --- | --- | --- |
| Host lifecycle | Ansible | Reproducibly configures DGX OS prerequisites, ConnectX-7 networking, K3s, labels, and bootstrap operators. |
| Cluster | K3s | Supplies the Kubernetes API and scheduling model with less operational weight than a conventional distribution. |
| GPU integration | NVIDIA GPU Operator | Advertises and monitors the Spark GPU through NVIDIA's supported ARM64 Kubernetes path. |
| Distributed lifecycle | KubeRay | Creates, heals, and removes Ray clusters used by multi-Spark inference. |
| Inference engine | vLLM | Provides efficient generation, embeddings, and an OpenAI-compatible backend. |
| Batch admission | Kueue | Holds a distributed job until all requested Sparks can start together. |
| API gateway | Apache APISIX | Keeps one stable endpoint while enforcing identity, model access, and quotas. |
| Identity | Microsoft Entra ID | Gives people SSO and automation separate, revocable identities. |
| Delivery | GitHub Actions and Argo CD | Validates reviewed intent, applies it, detects drift, and makes rollback a Git operation. |

Ansible owns hosts and cluster prerequisites. Argo CD owns namespaced
applications. Neither tool should manage resources owned by the other.

## Request path

```text
OpenAI-compatible client
        |
        | Entra access token
        v
      APISIX
        |
        | authorized model route
        v
Kubernetes Service
        |
        +--> standalone vLLM Deployment
        |
        `--> KubeRay RayService --> vLLM workers on 2+ Sparks
```

APISIX validates token signature, issuer, audience, expiry, delegated scope, and
fleet role. It returns only the models assigned to that role, applies per-team and
per-model quotas, and routes requests to healthy Services. The public model ID
does not change when its placement changes.

## Delivery path

1. An operator changes `catalog/models/`, `teams.yaml`, `versions.yml`, or a cluster
   profile in a pull request.
2. CI validates schemas, immutable pins, capacity, topology, gateway policy, and
   Ansible syntax.
3. The release workflow renders an immutable Kubernetes artifact.
4. Argo CD applies and reconciles the approved artifact.
5. Probes keep traffic away from a model until its backend is healthy.
6. Reverting the release commit restores the previous desired state.

Model acquisition is a separate promotion gate. An approved security runner is
the only component allowed to reach Hugging Face through the corporate proxy. It
publishes a deterministic, checksummed artifact to the internal model store;
per-node staging Jobs verify and atomically install it before a serving workload
starts. Runtime pods have no Hugging Face credentials and run in offline mode.

## Eight-Spark control plane

The initial lower environment runs one schedulable K3s server on `spark-01` and
seven agents. System services do not claim its GPU, so it remains usable for
inference. This minimizes maintenance but accepts a temporary control-plane
outage if that Spark fails; existing workloads may continue while new scheduling
and GitOps reconciliation stop.

K3s datastore snapshots must be copied outside the fleet. If availability later
requires quorum, promote `spark-03` and `spark-05` to schedulable server members.
That spreads three control-plane members across distinct direct pairs and does not
require additional machines.

## Capacity and workload policy

| Mode | Inference | Training | Use |
| --- | ---: | ---: | --- |
| Normal | 6 Sparks | 2 Sparks | Daily service and small fine-tuning jobs |
| Inference peak | 8 Sparks | 0 Sparks | Traffic spike, large rollout, or recovery |
| Training burst | 4 Sparks | 4 Sparks | Approved, checkpointed distributed training |

Node labels express preference, not permanent ownership. Kueue quotas protect the
six-Spark inference floor in normal mode and admit multi-node work only when its
entire requested width is available. Preemptible overflow inference may use idle
training-preferred nodes, but must tolerate eviction before a scheduled job.

## Fabric-independent application model

The repository provides two cluster profiles:

- `clusters/dgx-spark/cluster.yaml` describes four direct two-node pairs.
- `clusters/dgx-spark-switched/cluster.yaml` describes one shared eight-node fabric.

Everything above the physical fabric remains the same. In the pair profile,
`fabric_group` limits a distributed placement to one pair. In the switched
profile, the common fabric group makes every healthy node eligible and gang
admission chooses the requested number. See [Fabric topologies](TOPOLOGIES.md)
for the detailed tradeoff.

## Data and secrets

Model weights are cached on each Spark; the authoritative artifact lives in the
internal model store, while its immutable identity and checksums live in Git.
Training checkpoints and release artifacts must live outside the fleet so a
rebuild does not destroy them. Secrets are supplied through the enterprise secret
manager or protected CI environment and are never rendered into committed files.

The [NVIDIA hardware guide](https://docs.nvidia.com/dgx/dgx-spark/hardware.html)
lists DGX Spark with either 1 TB or 4 TB of local NVMe. Cluster profiles keep
`model_storage.capacity_gb` null until hardware discovery records the purchased
SKU. An active model is rejected until its steady cache and largest temporary
staging archive fit the declared per-node budget.

## Promotion gates

The platform is not ready for service until:

- a digest-pinned ARM64 Ray Serve/vLLM image runs on DGX Spark;
- Entra, TLS, ingress, and role-filtered catalog tests pass;
- GPU Operator and KubeRay report healthy on all eight nodes;
- NCCL bandwidth and collectives pass at every permitted workload width;
- node loss, drain, rollback, checkpoint restore, and fabric failure are tested;
- APISIX, vLLM, Ray, Kubernetes, GPU, and quota metrics feed alerting;
- the selected topology's acceptance gates are recorded.

The image and model qualification process incorporates real DGX Spark lessons in
[DGX Spark vLLM runtime evidence](DGX_RUNTIME.md).
