# Model catalog

Open model lifecycle pull requests here. Each `<model-name>.yaml` file owns one
stable OpenAI-compatible model ID, and its filename must exactly match `name`.

## Add a model

1. Copy an existing entry and set `state: approved`.
2. Pin a 40-character Hugging Face commit; never use a tag or branch.
3. Record model ownership, platform approval, security approval, measured memory,
   serving arguments, and proposed placement.
4. Import it through the controlled process in
   [Model supply chain](../../docs/MODEL_SUPPLY_CHAIN.md).
5. Upload the deterministic artifact to an HTTPS location allowed by
   `model-store-policy.yml`.
6. Add `artifact_uri`, `artifact_sha256`, `manifest_sha256`, and measured
   `size_gb`; change the state to `active` in a reviewed promotion PR.

Approved models consume no fleet capacity and do not appear in `/v1/models`.
Active models must fit GPU and local NVMe admission checks for every eligible
Spark before CI permits rendering.

Team visibility and quotas remain separate in `teams.yaml`. Changing placement,
replica count, or tensor-parallel width does not change the public model ID.
