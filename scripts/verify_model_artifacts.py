#!/usr/bin/env python3
"""Require every active catalog artifact to exist in the internal model store."""

from __future__ import annotations

import argparse
import os
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleetplan  # noqa: E402


def probe(uri: str) -> None:
    request = urllib.request.Request(uri, method="HEAD")
    if token := os.environ.get("MODEL_STORE_BEARER_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    context = ssl.create_default_context(
        cafile=os.environ.get("MODEL_STORE_CA_BUNDLE") or None,
        cadata=os.environ.get("MODEL_STORE_CA_PEM") or None,
    )
    with urllib.request.urlopen(request, context=context) as response:
        if urllib.parse.urlparse(uri).scheme == "https" and urllib.parse.urlparse(response.geturl()).scheme != "https":
            raise ValueError("artifact probe redirected away from HTTPS")
        status = getattr(response, "status", None) or 200
        if status >= 400:
            raise ValueError(f"HTTP {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=fleetplan.MODEL_CATALOG)
    parser.add_argument(
        "--policy", type=Path,
        default=Path(__file__).resolve().parent.parent / "model-store-policy.yml",
    )
    args = parser.parse_args()
    policy = fleetplan.load_yaml(args.policy)
    prefixes = tuple(policy["allowed_artifact_prefixes"])
    errors = []
    for model in fleetplan.load_models(args.models)["models"]:
        if model.get("state") != "active":
            continue
        try:
            if not model["artifact_uri"].startswith(prefixes):
                raise ValueError("URI is outside model-store-policy.yml")
            probe(model["artifact_uri"])
            print(f"OK {model['name']}: {model['artifact_uri']}")
        except Exception as error:  # The release gate reports all missing artifacts together.
            errors.append(f"{model['name']}: {error}")
    for error in errors:
        print(f"ERROR {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
