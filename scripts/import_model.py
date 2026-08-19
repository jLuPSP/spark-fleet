#!/usr/bin/env python3
"""Import one approved Hugging Face revision into a deterministic model artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

import yaml

REMOTE_CODE_SUFFIXES = {".dll", ".dylib", ".py", ".so"}
UNSAFE_SERIALIZATION_SUFFIXES = {".bin", ".ckpt", ".pickle", ".pkl", ".pt", ".pth"}
IGNORED_PARTS = {".cache", "__pycache__"}
MANIFEST_NAME = ".spark-fleet-manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inventory(source: Path, model: dict) -> tuple[list[dict], int]:
    files = []
    total = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"symbolic links are not permitted in model artifacts: {relative}")
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in REMOTE_CODE_SUFFIXES and not model["requires_remote_code"]:
            raise ValueError(
                f"{relative} contains executable/remote code but requires_remote_code is false"
            )
        if suffix in UNSAFE_SERIALIZATION_SUFFIXES and not model["allow_unsafe_serialization"]:
            raise ValueError(
                f"{relative} uses unsafe serialization but allow_unsafe_serialization is false"
            )
        size = path.stat().st_size
        total += size
        files.append({"path": relative.as_posix(), "size": size, "sha256": sha256_file(path)})
    if not files:
        raise ValueError("download produced no model files")
    return files, total


def manifest_bytes(model: dict, files: list[dict], total: int) -> bytes:
    manifest = {
        "format": "spark-fleet-model/v1",
        "model": model["name"],
        "repo": model["repo"],
        "revision": model["revision"],
        "requires_remote_code": model["requires_remote_code"],
        "allow_unsafe_serialization": model["allow_unsafe_serialization"],
        "total_bytes": total,
        "files": files,
    }
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def create_artifact(source: Path, artifact: Path, manifest: bytes, files: list[dict]) -> None:
    """Create a deterministic, uncompressed tar; model weights rarely compress well."""
    with tarfile.open(artifact, "w", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest)
        info.mode = 0o644
        info.mtime = 0
        import io

        archive.addfile(info, io.BytesIO(manifest))
        for item in files:
            path = source / item["path"]
            info = archive.gettarinfo(str(path), arcname=item["path"])
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mode = 0o644
            info.mtime = 0
            with path.open("rb") as handle:
                archive.addfile(info, handle)


def download_snapshot(model: dict, destination: Path) -> None:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise RuntimeError("install requirements-import.txt before importing models") from error
    options = {
        "repo_id": model["repo"],
        "revision": model["revision"],
        "local_dir": destination,
        "token": os.environ.get("HF_TOKEN"),
    }
    if endpoint := os.environ.get("HF_ENDPOINT"):
        options["endpoint"] = endpoint
    snapshot_download(**options)


def import_model(model_file: Path, output: Path, source_dir: Path | None = None) -> dict:
    model = yaml.safe_load(model_file.read_text(encoding="utf-8"))
    if model.get("state") not in {"approved", "active"}:
        raise ValueError("model must be approved before import")
    output.mkdir(parents=True, exist_ok=True)

    temporary = None
    if source_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="spark-fleet-import-")
        source = Path(temporary.name)
        download_snapshot(model, source)
    else:
        source = source_dir

    try:
        files, total = inventory(source, model)
        manifest = manifest_bytes(model, files, total)
        base = f"{model['name']}-{model['revision']}"
        manifest_path = output / f"{base}.manifest.json"
        artifact_path = output / f"{base}.tar"
        manifest_path.write_bytes(manifest)
        create_artifact(source, artifact_path, manifest, files)
        result = {
            "artifact": str(artifact_path),
            "artifact_sha256": sha256_file(artifact_path),
            "manifest": str(manifest_path),
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "size_gb": round(total / 1_000_000_000, 6),
        }
        return result
    finally:
        if temporary is not None:
            temporary.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--source-dir", type=Path,
        help="package an already-downloaded directory (used for offline qualification/testing)",
    )
    parser.add_argument(
        "--artifact-base-url",
        help="print the activation fields using this internal HTTPS directory",
    )
    args = parser.parse_args()
    result = import_model(args.model_file, args.output, args.source_dir)
    if args.artifact_base_url:
        result["artifact_uri"] = (
            args.artifact_base_url.rstrip("/") + "/" + Path(result["artifact"]).name
        )
    print(yaml.safe_dump(result, sort_keys=False).rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
