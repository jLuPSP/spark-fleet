# User and operator scenarios

These scenarios describe the intended DGX Spark experience. Users interact with
identity, model IDs and jobs; physical Spark placement remains an operator detail.

## Interactive developer in Kilo Code

1. The platform team assigns the developer an Entra `Fleet.*` role.
2. The developer obtains a delegated token with the device-code helper.
3. Kilo receives the fleet base URL, that token as its API key and an authorized
   model ID.
4. `GET /v1/models` exposes only the developer's team catalog.
5. APISIX routes requests to healthy standalone vLLM or Ray Serve capacity.

Moving the model between Sparks or converting it to a distributed deployment does
not change the user's endpoint or model ID.

## Application or CI automation

An unattended service uses its own Entra confidential client, receives a
`Fleet.*` application role and obtains tokens through client credentials. It uses
the normal OpenAI SDK and handles `429` by respecting the response reset headers;
human credentials and automation secrets are never interchangeable.

## Checking quota

Every inference response can carry request and token limit, remaining and reset
headers. Kilo shows them in error details, raw HTTP clients can inspect them on
every response, and a planned `/fleet/v1/me` endpoint plus `spark-fleet quota`
command will provide a friendlier view. The current counters enforce quota; they
are not a durable billing or historical-usage ledger.

## Requesting and rolling out a model

The model owner supplies an immutable model revision, serving arguments, resource
estimate and approval. An operator adds `catalog/models/<model>.yaml`; an audited
runner imports it into the internal store. A second reviewed change activates the
verified artifact, CI proves placement and generated APISIX policy, and Argo CD
rolls out the release. Readiness gates traffic, and a Git revert restores the
previous revision.

## Single-Spark training

A data scientist submits a versioned job containing the base model, dataset,
recipe, resource request and output location. The job waits for Spark 7 or 8,
runs with an exclusive GPU, checkpoints outside the fleet, evaluates its output
and produces an immutable adapter or model artifact. Promotion into inference is
a separate reviewed catalog change.

## Two-Spark training

A distributed job remains queued until a complete eligible pair is available.
That means both members of one physical pair in the direct topology, or any two
admitted nodes in the switched topology. PyTorch FSDP communicates through NCCL,
and the job checkpoints so it can recover from node loss or release capacity for
inference. Both Sparks return to the shared pool when the job completes.

## Four- or eight-Spark model

An operator selects a previously qualified runtime profile containing the exact
model revision, image digest, parallelism, parsers, memory settings, and fabric
environment. Kueue admits every requested node together. The model prewarms from
checksummed host caches, passes OpenAI protocol and performance tests, and only
then receives APISIX traffic. This scenario requires the switched topology.

## Changing the 75/25 split

Normal operation reserves six Sparks for inference and two for training. In the
direct topology that normally maps to Pairs A–C and Pair D. In the switched
topology it is a queue reservation selected from one shared pool. Users retain the
same API endpoint because APISIX routes model IDs rather than physical nodes.

## Recovering the control-plane Spark

If Spark 1 fails, already-running inference may continue but cluster changes pause.
The operator reimages the Spark, applies Ansible, restores or recreates cluster
state, and lets Argo CD reconcile the release. If that outage becomes unacceptable,
Sparks 3 and 5 are promoted to K3s server members for on-fleet quorum.
