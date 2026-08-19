"""Controlled model import, verification, and staging are deterministic and offline-safe."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import fleetplan  # noqa: E402
import import_model  # noqa: E402
import stage_model  # noqa: E402
import verify_model_artifacts  # noqa: E402


def model_file(tmp_path: Path, **updates) -> Path:
    model = fleetplan.load_models()["models"][0]
    model.update(updates)
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump(model, sort_keys=False), encoding="utf-8")
    return path


def safe_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.json").write_text('{"model_type":"test"}\n', encoding="utf-8")
    (source / "model.safetensors").write_bytes(b"safe-weight-fixture")
    return source


def test_import_is_deterministic_and_stage_verifies_every_file(tmp_path):
    model_path = model_file(tmp_path)
    source = safe_source(tmp_path)
    first = import_model.import_model(model_path, tmp_path / "first", source)
    second = import_model.import_model(model_path, tmp_path / "second", source)
    assert first["artifact_sha256"] == second["artifact_sha256"]
    assert first["manifest_sha256"] == second["manifest_sha256"]

    model = yaml.safe_load(model_path.read_text())
    destination = stage_model.stage(
        Path(first["artifact"]).resolve().as_uri(),
        first["artifact_sha256"],
        first["manifest_sha256"],
        tmp_path / "models",
        model["name"],
        model["revision"],
    )
    assert (destination / "model.safetensors").read_bytes() == b"safe-weight-fixture"
    manifest = json.loads((destination / stage_model.MANIFEST_NAME).read_text())
    assert manifest["revision"] == model["revision"]

    (destination / "model.safetensors").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="failed verification"):
        stage_model.stage(
            Path(first["artifact"]).resolve().as_uri(),
            first["artifact_sha256"],
            first["manifest_sha256"],
            tmp_path / "models",
            model["name"],
            model["revision"],
        )


def test_import_rejects_unapproved_remote_code_and_pickle_weights(tmp_path):
    source = safe_source(tmp_path)
    (source / "modeling_custom.py").write_text("raise SystemExit\n", encoding="utf-8")
    with pytest.raises(ValueError, match="requires_remote_code"):
        import_model.import_model(model_file(tmp_path), tmp_path / "remote-code", source)

    (source / "modeling_custom.py").unlink()
    (source / "pytorch_model.bin").write_bytes(b"pickle-compatible")
    with pytest.raises(ValueError, match="unsafe serialization"):
        import_model.import_model(model_file(tmp_path), tmp_path / "pickle", source)


def test_artifact_probe_accepts_local_fixture(tmp_path):
    artifact = tmp_path / "artifact.tar"
    artifact.write_bytes(b"fixture")
    verify_model_artifacts.probe(artifact.resolve().as_uri())


def test_staging_rejects_insufficient_free_space(tmp_path, monkeypatch):
    class Usage:
        free = 10_000_000_000

    monkeypatch.setattr(stage_model.shutil, "disk_usage", lambda _: Usage())
    with pytest.raises(ValueError, match="insufficient model staging space"):
        stage_model.require_staging_space(tmp_path, size_gb=10, minimum_free_gb=2)
