"""Resolve image tags in versions.yml to sha256 manifest digests.

Talks to the registries' HTTP APIs directly (stdlib only), so it works without a
docker daemon: Docker Hub, quay.io, ghcr.io, nvcr.io. Rewrites only the digest lines
in versions.yml, preserving comments and formatting.

Usage:
  python scripts/pin_digests.py            # fill in digests that are null
  python scripts/pin_digests.py --all      # re-resolve every digest
  python scripts/pin_digests.py --check    # exit 1 if any digest is stale or null
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
VERSIONS = REPO / "versions.yml"

MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
    ]
)


def _get(url: str, headers: dict) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def _registry_for(repo: str) -> tuple[str, str]:
    """Split a registry-qualified image name into (registry, path)."""
    first = repo.split("/", 1)[0]
    if "." in first or ":" in first:
        registry, path = repo.split("/", 1)
        return registry, path
    # Docker Hub; bare names like 'vllm/vllm-openai' or official 'python'
    path = repo if "/" in repo else f"library/{repo}"
    return "registry-1.docker.io", path


def _token(registry: str, path: str, www_auth: str | None) -> str | None:
    """Anonymous pull token via the auth challenge (RFC 6750 style)."""
    if www_auth:
        fields = dict(re.findall(r'(\w+)="([^"]*)"', www_auth))
        realm, service = fields.get("realm"), fields.get("service", registry)
    elif registry == "registry-1.docker.io":
        realm, service = "https://auth.docker.io/token", "registry.docker.io"
    else:
        return None
    if not realm:
        return None
    query = urllib.parse.urlencode({"service": service, "scope": f"repository:{path}:pull"})
    status, _, body = _get(f"{realm}?{query}", {})
    if status != 200:
        return None
    return json.loads(body).get("token") or json.loads(body).get("access_token")


def resolve_digest(repo: str, tag: str) -> str:
    registry, path = _registry_for(repo)
    url = f"https://{registry}/v2/{path}/manifests/{tag}"
    headers = {"Accept": MANIFEST_TYPES}

    status, resp_headers, _ = _get(url, headers)
    if status == 401:
        token = _token(registry, path, resp_headers.get("www-authenticate"))
        if not token:
            raise RuntimeError(f"{repo}:{tag}: could not obtain anonymous pull token")
        headers["Authorization"] = f"Bearer {token}"
        status, resp_headers, _ = _get(url, headers)
    if status != 200:
        raise RuntimeError(f"{repo}:{tag}: registry returned HTTP {status}")

    digest = resp_headers.get("docker-content-digest")
    if not digest or not digest.startswith("sha256:"):
        raise RuntimeError(f"{repo}:{tag}: no Docker-Content-Digest header")
    return digest


def rewrite(lines: list[str], key: str, digest: str) -> None:
    """Replace the digest line inside the image block for `key`, in place."""
    in_block = False
    block_indent = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if re.match(rf"^{re.escape(key)}\s*:", stripped):
            in_block, block_indent = True, indent
            continue
        if in_block:
            if stripped and indent <= block_indent:
                break  # left the block without finding digest
            if re.match(r"^digest\s*:", stripped):
                lines[i] = " " * indent + f'digest: "{digest}"\n'
                return
    raise RuntimeError(f"versions.yml: could not find digest line for images.{key}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="re-resolve every digest")
    parser.add_argument("--check", action="store_true", help="verify, change nothing")
    args = parser.parse_args()

    doc = yaml.safe_load(VERSIONS.read_text(encoding="utf-8"))
    lines = VERSIONS.read_text(encoding="utf-8").splitlines(keepends=True)

    failures = 0
    stale = 0
    for key, image in sorted((doc.get("images") or {}).items()):
        current = image.get("digest")
        if current and not args.all and not args.check:
            continue
        try:
            digest = resolve_digest(image["repo"], image["tag"])
        except RuntimeError as exc:
            print(f"ERROR: {exc}")
            failures += 1
            continue
        if args.check:
            if current != digest:
                print(f"STALE: images.{key}: {current} -> {digest}")
                stale += 1
            else:
                print(f"ok: images.{key} {digest}")
            continue
        rewrite(lines, key, digest)
        print(f"pinned: images.{key} = {image['repo']}:{image['tag']} @ {digest}")

    if not args.check and failures == 0:
        VERSIONS.write_text("".join(lines), encoding="utf-8", newline="")
    if args.check and stale:
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
