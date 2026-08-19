"""Test-only helpers that promote the publishable approved catalog in memory."""

from __future__ import annotations

import copy
import hashlib


def active_catalog(fleetplan) -> dict:
    catalog = copy.deepcopy(fleetplan.load_models())
    for model in catalog["models"]:
        digest = hashlib.sha256(model["name"].encode()).hexdigest()
        model.update({
            "state": "active",
            "artifact_uri": f"https://models.example.invalid/{model['name']}.tar",
            "artifact_sha256": digest,
            "manifest_sha256": hashlib.sha256((model["name"] + "-manifest").encode()).hexdigest(),
            "size_gb": model["vram_gb"],
        })
    return catalog
