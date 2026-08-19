# Model supply chain

## Security boundary

DGX Sparks and serving pods do not contact Hugging Face. Only a dedicated import
runner may cross the corporate proxy, and it receives a short-lived `HF_TOKEN`
from the enterprise secret manager. The runner downloads one pinned commit,
checks its file policy, creates a deterministic tar archive and manifest, and
publishes both to an internal HTTPS artifact store.

```text
Hugging Face -- approved proxy --> import runner --> scan and checksums
                                                       |
                                                       v
                                                internal model store
                                                       |
                                  +--------------------+--------------------+
                                  v                    v                    v
                              Spark cache          Spark cache          Spark cache
                                  |                    |                    |
                                  `---------------- offline vLLM / Ray ---------------'
```

Proxy credentials and the Hugging Face token never reach Kubernetes. The model
store credential may be delivered to staging Jobs through an externally managed
`spark-fleet-model-store` Secret; serving containers do not mount it. Supported
keys are `MODEL_STORE_BEARER_TOKEN`, standard proxy variables, and
`MODEL_STORE_CA_PEM` for a private certificate authority.

## Catalog lifecycle

Each model has one file under `catalog/models/`.

- `approved` means its identity, license, security review, resource estimate, and
  proposed placement are recorded. It is not rendered, routed, or returned from
  `/v1/models`.
- `active` additionally requires `artifact_uri`, `artifact_sha256`,
  `manifest_sha256`, `size_gb`, and placement. It renders staging Jobs and serving
  workloads.

This separation permits many approved models without pretending all of them fit
in the eight-Spark runtime. Activating a model remains a capacity-changing PR.

## Import an approved revision

Install importer-only dependencies on the controlled runner:

```bash
python3 -m venv .venv-import
.venv-import/bin/pip install -r requirements-import.txt
```

Configure its approved proxy and token outside Git:

```bash
export HTTPS_PROXY=PROXY_FROM_SECRET_MANAGER
export HF_TOKEN=SHORT_LIVED_TOKEN
```

Import the exact revision from the model file:

```bash
.venv-import/bin/python scripts/import_model.py \
  catalog/models/qwen3-14b-awq.yaml \
  --output build/model-import/qwen3-14b-awq \
  --artifact-base-url https://MODEL_STORE/models
```

Upload the emitted `.tar` and `.manifest.json` to the internal location. Copy
the printed artifact URI, artifact checksum, manifest checksum, and measured size
into the model file, then change its state to `active` in a reviewed PR.
The URI must begin with an HTTPS prefix allowed by `model-store-policy.yml`;
replace its non-routable example prefix during environment preparation.

The importer rejects symlinks, executable or Python files when
`requires_remote_code` is false, and pickle-compatible weight formats when
`allow_unsafe_serialization` is false. Any exception requires an explicit catalog
flag and security approval; prefer `safetensors`.

## CI and release gates

Ordinary pull-request CI validates schema, immutable revisions, security fields,
exclusive-GPU capacity, topology, and deterministic rendering without needing
internal network access. The release workflow runs on a self-hosted runner labeled
`spark-fleet-release`; it must reach every active `artifact_uri` before rendering
a deployable release.

The self-hosted runner receives `MODEL_STORE_BEARER_TOKEN` from a GitHub
environment secret. If the store uses a private CA, install it on the runner and
set the repository variable `MODEL_STORE_CA_BUNDLE` to that local file path.

## Node staging

Rendering creates one idempotent Kubernetes Job per active model and target node.
Argo sync wave `-1` waits for these Jobs before wave `0` serving workloads. Each
Job:

1. takes a host lock so only one archive stages on that Spark at a time;
2. requires room for the archive, extracted files, and configured free-space floor;
3. downloads only from the cataloged internal HTTPS URI;
4. verifies the archive SHA-256;
5. rejects path traversal, links, and non-regular archive members;
6. verifies the embedded manifest SHA-256 and every model file;
7. writes to a temporary directory;
8. atomically renames it to
   `/var/lib/spark-fleet/models/<model>/<revision>`.

The cluster profile records actual device capacity, a steady-cache utilization
ceiling, and minimum free space. `capacity_gb: null` is an intentional promotion
block until hardware discovery confirms whether the fleet has 1 TB or 4 TB
devices. CI checks each eligible node using the measured `size_gb` from import.

Ansible `playbooks/stage-models.yml` provides the same operation for deliberate
prewarming. External acquisition still happens once; per-node transfers originate
from the internal store, not Hugging Face. A future optimization may seed one
Spark and fan out across the high-speed fabric without changing the artifact
contract.

## Offline serving

Runtime containers mount `/var/lib/spark-fleet/models` read-only at `/models` and
receive `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and
`HF_DATASETS_OFFLINE=1`. Standalone vLLM and Ray both receive the same local path;
the public `--served-model-name` remains stable.

This removes proxy behavior from startup, prevents accidental branch-head pulls,
and fixes the prior mismatch where standalone vLLM received a revision but the
Ray model source did not.

## Container images and bootstrap artifacts

Model isolation alone is insufficient in a restricted network. Mirror all images
from `versions.yml`, K3s installation assets, NVIDIA packages, and Helm charts to
approved internal repositories. Configure K3s `registries.yaml` through the
private `k3s_registry_config` inventory variable and use `enterprise_proxy_env`
only for bootstrap URLs that have not yet been mirrored.

No internal hostname, proxy URL, credential, CA material, or token belongs in
this repository.

## Rollback and cleanup

A rollback points the catalog at the prior immutable artifact and lets staging
verify it before traffic moves. Do not delete the previous revision during its
rollback window. Garbage collection is a separate reviewed operation that removes
only revisions absent from the active catalog and older than the retention policy.
