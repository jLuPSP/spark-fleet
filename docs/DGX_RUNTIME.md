# DGX Spark vLLM runtime evidence

## Why this exists

The community project
[`eugr/spark-vllm-docker`](https://github.com/eugr/spark-vllm-docker) is useful
implementation evidence: it records real ARM64/GB10 builds, direct and switched
clusters, model-specific recipes, cache behavior, and failure workarounds. Its
Docker/SSH launcher is intentionally not adopted as the fleet control plane;
Kubernetes, KubeRay, APISIX, Entra, and GitOps remain the enterprise design.

Community results prove that a specific image, model, topology, and argument set
worked together. They do not turn a nightly image or experimental patch into an
approved platform dependency.

## Transferable lessons

| Observed experience | Fleet incorporation |
| --- | --- |
| ARM64/GB10 often needs a tested vLLM build, FlashInfer/CUTLASS combination, or model patch. | Build an internal image from pinned commits, scan it, publish by digest, and qualify it per model. Never promote a floating community `latest` tag. |
| A single cable exposes two Ethernet and two RoCE interfaces. Full NCCL RDMA uses both RoCE HCAs. | Cluster profiles set `NCCL_SOCKET_IFNAME` and both values in `NCCL_IB_HCA`; Ansible verifies the corresponding interfaces. |
| The two Ethernet twins must not use the same subnet. | Both inventories assign distinct subnets to the two logical interfaces. |
| Tensor-parallel widths commonly follow 1, 2, 4, and 8. | Admission and qualification use those widths; an unusual width needs explicit evidence. |
| Model files are downloaded once and copied before launch. | Import one immutable revision through the audited proxy path, publish it internally, and pre-stage it on every eligible Spark. Runtime internet downloads are forbidden. |
| Unified memory makes percentage-based GPU settings and low cgroup limits risky. | K3s reserves memory for the host, GPU pods have no artificially small memory limit, and every model records measured startup/KV headroom. |
| `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` reduces allocator fragmentation. | Rendered standalone and Ray workers receive that environment setting by default. |
| Tool and reasoning parsers are model-specific. | Qualification must exercise automatic tool choice and record the exact parser flags in the per-model catalog file. |
| Fast loading formats improve startup but can temporarily consume more memory. | Loading format is treated as a measured model setting; use the conservative default until peak startup memory is proven. |
| Native vLLM multiprocessing can outperform or simplify some multi-node cases, while other recipes still use Ray. | KubeRay remains the lifecycle default, but every major model benchmarks Ray against native vLLM before the backend is frozen. |
| Four- and eight-node recipes exist, including TP=8, but carry precise kernels and environment settings. | Wider serving is allowed only for the exact qualified runtime profile; success is not generalized to every model. |

The networking observations align with NVIDIA's documented ConnectX-7 interface
mapping. The community guide additionally demonstrates why both RoCE twins must
be passed to NCCL and why cache/model distribution matters operationally.

## Internal image pipeline

The current ARM64 image gap should be closed with a controlled build, not by
running an unpinned public nightly:

1. Select a vLLM release or exact commit known to support GB10/sm12x.
2. Record exact PyTorch, CUDA, FlashInfer, CUTLASS, NCCL, and optional kernel refs.
3. Build on an isolated ARM64 runner from reviewed source.
4. Generate an SBOM, scan the image, and run unit/protocol tests.
5. Run single-Spark smoke and memory tests.
6. Run direct two-Spark and switched 2/4-node tests.
7. Push the immutable digest to the enterprise registry.
8. Update `versions.yml` and attach the qualification record to the pull request.

Experimental mods are separate image variants. They are never patched into a
running pod or downloaded from an unpinned branch during startup.

## Model qualification record

Treat the community recipe concept as evidence attached to the existing catalog.
For each model, record at least:

```yaml
model: ORG/MODEL
model_revision: IMMUTABLE_COMMIT
image_digest: sha256:...
topology: switched
node_count: 4
parallelism: { tensor: 4, pipeline: 1, data: 1 }
engine:
  context_length: 32768
  max_sequences: 8
  max_batched_tokens: 8192
  gpu_memory_utilization: 0.78
  kv_cache_dtype: fp8
  tool_parser: MODEL_SPECIFIC
environment:
  NCCL_SOCKET_IFNAME: DISCOVERED_INTERFACE
  NCCL_IB_HCA: DISCOVERED_HCA_1,DISCOVERED_HCA_2
evidence:
  startup_peak_memory_gb: MEASURED
  steady_memory_gb: MEASURED
  time_to_ready_seconds: MEASURED
  prompt_tokens_per_second: MEASURED
  generation_tokens_per_second: MEASURED
  concurrent_request_count: MEASURED
  node_loss_result: PASS_OR_FAIL
```

The approved values then flow into `catalog/models/`, `versions.yml`, and the matching
cluster profile. Performance observations remain attached evidence rather than
unreviewed comments in a launch script.

## Model staging workflow

1. Resolve the Hugging Face revision and verify its expected files/checksums.
2. Download once through an authenticated, audited job.
3. Copy to a temporary path on every eligible node over the fabric.
4. Verify file count, size, and checksum on every node.
5. Atomically rename the completed directory into the shared cache path.
6. Only then allow Argo CD/Kueue to admit the model.

This prevents eight simultaneous internet downloads, inconsistent revisions, and
pods becoming partially ready while weights are still moving.
The implemented commands and credential boundaries are documented in
[Model supply chain](MODEL_SUPPLY_CHAIN.md).

## Qualification scenarios

Every serving profile must test:

- `/v1/models`, chat, completions, embeddings where applicable, and streaming;
- automatic tool choice with the exact parser;
- requested context and maximum output-token boundaries;
- cold start, warm start, and first-request compilation;
- one, several, and saturation-level concurrent requests;
- allocator fragmentation across repeated load/unload cycles;
- model-cache absence, corruption, and permission failure;
- one worker loss, Ray/native backend recovery, and complete rollback;
- NCCL behavior with both HCAs and with one degraded path.

The community repository's real eight-Spark recipe is encouraging evidence that
TP=8 can work. Our acceptance criterion is still a repeatable result on the
selected switch, DGX OS, driver, internal image, and exact model revision.
