#!/usr/bin/env python3
"""Render the active model/node matrix consumed by the Ansible staging playbook."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

import fleetplan

REPO = Path(__file__).resolve().parent.parent


def render(models_path: Path, profile_path: Path) -> dict:
    models = fleetplan.load_models(models_path)
    profile = fleetplan.load_yaml(profile_path)
    minimum_free_gb = profile["cluster"]["model_storage"]["minimum_free_gb"]
    topology = fleetplan.topology_from_profile(profile)
    physical = {
        logical: node["kubernetes_node"]
        for logical, node in profile["cluster"]["nodes"].items()
    }
    result = []
    for model in sorted(models["models"], key=lambda item: item["name"]):
        if model.get("state") != "active":
            continue
        targets = set()
        for placement in model["placement"]:
            targets.update(
                fleetplan.eligible_placement_nodes(
                    topology,
                    placement["node"],
                    int(placement.get("tensor_parallel", 1)),
                )
            )
        result.append({
            "name": model["name"],
            "revision": model["revision"],
            "artifact_uri": model["artifact_uri"],
            "artifact_sha256": model["artifact_sha256"],
            "manifest_sha256": model["manifest_sha256"],
            "size_gb": model["size_gb"],
            "minimum_free_gb": minimum_free_gb,
            "target_hosts": sorted(physical[name] for name in targets),
        })
    return {"model_stage_models": result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=fleetplan.MODEL_CATALOG)
    parser.add_argument(
        "--profile", type=Path,
        default=REPO / "clusters" / "dgx-spark" / "cluster.yaml",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        yaml.safe_dump(render(args.models, args.profile), sort_keys=False),
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
