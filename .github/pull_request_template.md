## Change

Describe the operational intent and affected topology.

## Model evidence

- [ ] Model revision is an immutable 40-character commit.
- [ ] License, platform owner, and security approval are recorded.
- [ ] Internal artifact and manifest checksums came from the audited importer.
- [ ] Remote code or unsafe serialization exceptions are explicitly approved.
- [ ] GPU memory, local NVMe, temporary staging, replicas, and width fit.
- [ ] Tool parser, context boundary, streaming, and rollback tests are attached.

Mark non-model items not applicable and explain why.

## Validation

- [ ] `make check`
- [ ] Both topology profiles validate.
- [ ] Generated release diff was reviewed.
- [ ] No address, credential, tenant value, kubeconfig, or private inventory is included.
