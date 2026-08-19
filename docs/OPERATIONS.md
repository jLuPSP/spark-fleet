# Operations

## Ownership

- Ansible changes DGX hosts, fabric interfaces, K3s, and bootstrap operators.
- Git catalogs and cluster profiles describe application intent.
- CI validates and renders a release.
- Argo CD applies and reconciles namespaced applications.
- APISIX preserves the client endpoint during backend changes.

Avoid routine SSH changes or `kubectl edit`. An emergency change is followed by a
Git reconciliation so the repository remains the source of truth.

## Private inputs

Create ignored working files from committed templates:

| Template | Private file |
| --- | --- |
| `inventory/dgx-spark-pairs.example.yml` | `inventory/dgx-spark-pairs.private.yml` |
| `inventory/dgx-spark-switched.example.yml` | `inventory/dgx-spark-switched.private.yml` |
| `gateway/auth.prod.yml.example` | `gateway/auth.prod.yml` |
| `gateway/entra.user.env.example` | `private/entra.user.env` |
| `gateway/entra.automation.env.example` | `private/entra.automation.env` |

Real addresses, tenant identifiers, kubeconfigs, tokens, and secrets stay in
ignored files or the enterprise secret manager.

## Initial deployment

1. Provision and update all eight DGX Sparks through the enterprise hardware
   process.
2. Establish management DNS/IP access and a common administrative identity.
3. Cable the selected topology and prepare the private inventory.
4. Follow [Ansible deployment](ANSIBLE.md) to configure fabric and K3s.
5. Run topology acceptance tests from [Fabric topologies](TOPOLOGIES.md).
6. Configure Entra, TLS, ingress, secret delivery, and the Argo CD application.
7. Render and promote the matching cluster profile.
8. Run the automation and human-device-code endpoint checks.

## Routine change flow

| Change | Source | Result |
| --- | --- | --- |
| Model revision or engine flags | `catalog/models/<model>.yaml` | Affected Deployment or RayService rolls |
| Placement or width | Model file and cluster profile | Scheduler selects a new eligible set |
| Team access or quota | `teams.yaml` | APISIX policy updates |
| Platform/image version | `versions.yml` | Pinned consumers roll after review |
| Fabric eligibility | Cluster profile and inventory | Maintenance plus scheduling change |
| DGX OS, toolkit, or K3s | Ansible | Explicit host maintenance window |

Before opening a pull request:

```bash
make validate
make test
make lint
```

Review the source diff and generated release artifact. After Argo CD reports
healthy, verify the role-filtered catalog, chat, completion, and embedding routes:

```bash
python3 scripts/harness_check.py
```

## Capacity modes

| Mode | Inference guarantee | Training allowance | Approval |
| --- | ---: | ---: | --- |
| Normal | 6 Sparks | 2 Sparks | Default |
| Inference peak | 8 Sparks | 0 Sparks | Operator change |
| Training burst | 4 Sparks | 4 Sparks | Scheduled change |

Training must checkpoint externally and request all of its nodes through gang
admission. Low-priority inference may consume unused training preference but must
move before an admitted training window.

With direct pairs, admission allocates a complete physical pair. With the switch,
admission allocates the requested number from the shared fabric. The user-facing
model ID and endpoint remain unchanged.

## Model rollout

1. Add an `approved` per-model catalog file with a pinned revision and approvals.
2. Import once through the audited runner and publish the artifact internally.
3. Add its URI, artifact checksum, manifest checksum, and measured size.
4. Change `state` to `active`; CI proves placement and artifact reachability.
5. Argo staging Jobs verify and atomically install the revision on eligible Sparks.
6. Add a canary placement and wait for readiness.
7. Send representative protocol, tool-call, context, and latency tests.
8. Shift APISIX traffic only after the new Service is healthy.
9. Remove the prior placement after the rollback window.

Large models progress through widths 1, 2, and 4. Width 8 is enabled only after
its NCCL and failure tests meet a recorded baseline.
Use the qualification template in [Runtime evidence](DGX_RUNTIME.md).
The exact import and staging commands are in [Model supply chain](MODEL_SUPPLY_CHAIN.md).

## Node maintenance

1. Verify another eligible placement has capacity.
2. Stop new queue admission to the affected fabric group.
3. Cordon the Spark and move workloads through Git.
4. Wait for replacement readiness, then drain the node.
5. Apply DGX OS, firmware, Ansible, or hardware maintenance.
6. Verify management, ConnectX carrier, GPU Operator, and NCCL health.
7. Uncordon and restore placement through Git.

A tensor-parallel placement is healthy only when all workers and its serving
endpoint are healthy.

## Failure handling

| Failure | Expected response |
| --- | --- |
| Model process | Kubernetes/KubeRay restarts it; APISIX routes only to ready Services |
| One Spark | Drain or fence it, then reschedule within eligible capacity |
| Direct cable | Isolate the affected pair and use another complete pair |
| Switched port | Remove that Spark from admission until carrier/NCCL recover |
| Shared switch | Preserve management control, stop distributed admission, restore switch configuration |
| Initial K3s server | Restore from external snapshot or promote the documented recovery server |
| Bad release | Revert the Git release and let Argo CD reconcile |

## Identity and quotas

Human access changes through Entra group/app-role assignment. Automation uses a
separate confidential client with the minimum role. Rotate the automation secret
in Entra and the secret manager together.

APISIX returns request and token allowance headers. Operators correlate those
limits with Entra role, model route, gateway metrics, and queue state; quotas do
not attach to a physical Spark.

## Backups

Keep these outside the eight-node fleet:

- K3s datastore snapshots;
- switch configuration and approved cable/port map;
- private inventory in an enterprise secret/configuration store;
- training checkpoints and datasets;
- immutable release manifests and model revision metadata;
- APISIX/Entra configuration recovery records.

Local NVMe model directories are reconstructable caches, not backups. Monitor
free space on every Spark, retain the prior revision through its rollback window,
and delete an inactive revision only through a reviewed garbage-collection job.

Quarterly recovery exercises should rebuild a node, restore K3s control, restore
the switch configuration, and roll back a model without relying on undocumented
state.
