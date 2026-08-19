#!/usr/bin/env python3
"""Download, verify, and atomically stage an internal model artifact on one node."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import ssl
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

MANIFEST_NAME = ".spark-fleet-manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(uri: str, destination: Path) -> None:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme not in {"https", "file"}:
        raise ValueError("artifact URI must use HTTPS (file:// is accepted only for local testing)")
    request = urllib.request.Request(uri)
    if token := os.environ.get("MODEL_STORE_BEARER_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    context = ssl.create_default_context(
        cafile=os.environ.get("MODEL_STORE_CA_BUNDLE") or None,
        cadata=os.environ.get("MODEL_STORE_CA_PEM") or None,
    )
    with urllib.request.urlopen(request, context=context) as response, destination.open("wb") as out:
        final_scheme = urllib.parse.urlparse(response.geturl()).scheme
        if parsed.scheme == "https" and final_scheme != "https":
            raise ValueError("artifact download redirected away from HTTPS")
        shutil.copyfileobj(response, out, length=1024 * 1024)


def safe_extract(artifact: Path, destination: Path) -> None:
    root = destination.resolve()
    with tarfile.open(artifact, "r") as archive:
        for member in archive.getmembers():
            if not member.isfile() or member.issym() or member.islnk():
                raise ValueError(f"artifact member must be a regular file: {member.name}")
            target = (destination / member.name).resolve()
            if root not in target.parents:
                raise ValueError(f"artifact path escapes destination: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"could not read artifact member: {member.name}")
            with source, target.open("wb") as out:
                shutil.copyfileobj(source, out, length=1024 * 1024)
            target.chmod(0o644)


def verify_directory(path: Path, model: str, revision: str, manifest_sha256: str) -> None:
    manifest_path = path / MANIFEST_NAME
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ValueError("embedded model manifest checksum does not match the catalog")
    manifest = json.loads(raw)
    if manifest.get("model") != model or manifest.get("revision") != revision:
        raise ValueError("embedded model manifest identity does not match the catalog")
    expected = {item["path"] for item in manifest["files"]}
    actual = {
        item.relative_to(path).as_posix()
        for item in path.rglob("*")
        if item.is_file() and item.name != MANIFEST_NAME
    }
    if actual != expected:
        raise ValueError("staged model file set does not match the manifest")
    for item in manifest["files"]:
        candidate = path / item["path"]
        if candidate.stat().st_size != item["size"] or sha256_file(candidate) != item["sha256"]:
            raise ValueError(f"staged model file failed verification: {item['path']}")


def require_staging_space(root: Path, size_gb: float, minimum_free_gb: float) -> None:
    free_gb = shutil.disk_usage(root).free / 1_000_000_000
    required_gb = (2 * size_gb) + minimum_free_gb
    if free_gb < required_gb:
        raise ValueError(
            f"insufficient model staging space: {free_gb:.1f} GB free, "
            f"{required_gb:.1f} GB required for archive, extraction, and reserve"
        )


def stage(
    artifact_uri: str,
    artifact_sha256: str,
    manifest_sha256: str,
    root: Path,
    model: str,
    revision: str,
    size_gb: float = 0,
    minimum_free_gb: float = 0,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    destination = root / model / revision
    # Argo may create several model Jobs at one sync wave. Serialize work per
    # host so concurrent archives cannot consume the same free-space allowance.
    with (root / ".stage.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists():
            verify_directory(destination, model, revision, manifest_sha256)
            return destination
        require_staging_space(root, size_gb, minimum_free_gb)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=root) as temporary:
            work = Path(temporary)
            artifact = work / "model.tar"
            extracted = work / "content"
            extracted.mkdir()
            download(artifact_uri, artifact)
            if sha256_file(artifact) != artifact_sha256:
                raise ValueError("model artifact checksum does not match the catalog")
            safe_extract(artifact, extracted)
            verify_directory(extracted, model, revision, manifest_sha256)
            try:
                os.replace(extracted, destination)
            except OSError:
                if not destination.exists():
                    raise
                verify_directory(destination, model, revision, manifest_sha256)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-uri", required=True)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--size-gb", required=True, type=float)
    parser.add_argument("--minimum-free-gb", required=True, type=float)
    parser.add_argument("--root", type=Path, default=Path("/var/lib/spark-fleet/models"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    path = stage(
        args.artifact_uri, args.artifact_sha256, args.manifest_sha256,
        args.root, args.model, args.revision, args.size_gb, args.minimum_free_gb,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
