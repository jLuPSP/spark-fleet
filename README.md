# spark-fleet

`spark-fleet` is a reproducible platform for operating eight NVIDIA DGX Spark
systems behind one Entra-protected, OpenAI-compatible API.

Users choose a model, not a machine. Operators describe models, access, quotas,
and placement in Git; the platform schedules the requested inference or training
work across the available Sparks.

## The plan

```text
Users and tools
      |
      v
Microsoft Entra ID -> Apache APISIX -> vLLM / KubeRay
                                           |
                                           v
                                  eight DGX Sparks

Git pull request -> CI -> Argo CD -> K3s
Ansible --------------------------> hosts and fabric
```

The normal capacity policy reserves six Sparks for inference and two for
training. Operators can temporarily move to eight-node inference or a four-node
training burst through a reviewed Git change.

Two physical network designs are supported:

- **Four direct pairs:** simpler and fault-isolated; distributed jobs naturally
  use one two-Spark pair.
- **One switched fabric:** all eight Sparks form a shared high-speed pool; this
  is the recommended target because it reduces stranded capacity and permits
  wider models and training jobs.

The software architecture is identical in both designs. Only fabric
configuration, scheduling scope, and failure handling change.

## Stack

| Layer | Tool |
| --- | --- |
| Host and fabric configuration | Ansible |
| Cluster | K3s / Kubernetes |
| GPU integration | NVIDIA GPU Operator |
| Distributed lifecycle | KubeRay and Ray Serve |
| Model serving | vLLM |
| API gateway and quotas | Apache APISIX |
| User identity | Microsoft Entra ID |
| GitOps | GitHub Actions and Argo CD |

## Repository

| Path | Purpose |
| --- | --- |
| `clusters/` | Renderable DGX Spark topology profiles |
| `inventory/` | Placeholder Ansible inventories for both fabric designs |
| `playbooks/` | DGX host, fabric, K3s, and bootstrap automation |
| `catalog/models/` | One reviewed file per model, including lifecycle and placement |
| `model-store-policy.yml` | Allowed internal HTTPS artifact locations |
| `teams.yaml` | Entra roles, model access, and quotas |
| `gateway/` | APISIX and Entra policy generation |
| `gitops/` | Argo CD handoff |
| `docs/` | Detailed architecture, operations, authentication, and user guidance |

Real addresses, usernames, tenant IDs, secrets, kubeconfigs, and private
inventories are intentionally absent. Copy an `.example` file to an ignored
private file before deployment.

## Validate

```bash
make setup
make check
```

Activating a distributed DGX model remains deliberately blocked until a
digest-pinned ARM64 Ray Serve LLM image is supplied. Validation still proves the
catalogs, both cluster profiles, gateway policy, Ansible syntax, and deterministic
output.

Sample models are `approved`, not `active`: they are intentionally absent from
`/v1/models` until an audited importer publishes a checksummed artifact to the
enterprise model store. Active serving pods are offline-only and read verified
weights from each Spark's local cache.

Cluster profiles deliberately leave local NVMe capacity unset until discovery
confirms the 1 TB or 4 TB hardware SKU. CI blocks activation when model cache and
temporary staging headroom do not fit the recorded capacity.

Model owners begin with the [catalog contribution guide](catalog/models/README.md);
the pull-request template carries the promotion and security checklist.

Start with [Architecture](docs/ARCHITECTURE.md), compare the two designs in
[Fabric topologies](docs/TOPOLOGIES.md), and follow the
[Ansible deployment guide](docs/ANSIBLE.md). Day-2 procedures are in
[Operations](docs/OPERATIONS.md); [Authentication](docs/AUTH.md) and the
[User guide](docs/USER_GUIDE.md) cover the client experience. Real-world DGX
Spark/vLLM lessons and the internal image qualification path are captured in
[Runtime evidence](docs/DGX_RUNTIME.md). The proxy-safe acquisition and staging
design is in [Model supply chain](docs/MODEL_SUPPLY_CHAIN.md).
