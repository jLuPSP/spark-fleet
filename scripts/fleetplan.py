"""Turn a model catalog and Kubernetes cluster profile into a stable fleet plan."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

# First serving container on a node listens on BASE_PORT + 1, the next on + 2, etc.
BASE_PORT = 8000
MAX_CONTAINERS_PER_NODE = 99
MODEL_CATALOG = Path(__file__).resolve().parent.parent / "catalog" / "models"

def load_yaml(path: str | Path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_models(path: str | Path = MODEL_CATALOG) -> dict:
    """Load the per-model catalog directory or a legacy aggregate test fixture."""
    source = Path(path)
    if source.is_file():
        document = load_yaml(source)
        if not isinstance(document, dict) or not isinstance(document.get("models"), list):
            raise ValueError(f"{source}: aggregate model catalog must contain a models list")
        return document
    if not source.is_dir():
        raise FileNotFoundError(source)

    model_paths = sorted((*source.glob("*.yaml"), *source.glob("*.yml")))
    if not model_paths:
        raise ValueError(f"{source}: no .yaml or .yml model documents found")
    models = []
    for model_path in model_paths:
        document = load_yaml(model_path)
        if not isinstance(document, dict):
            raise ValueError(f"{model_path}: model document must be a mapping")
        if "models" in document:
            raise ValueError(f"{model_path}: per-model documents must not contain a models wrapper")
        if document.get("name") != model_path.stem:
            raise ValueError(
                f"{model_path}: filename must match model name {document.get('name')!r}"
            )
        models.append(document)
    return {"models": models}


def topology_from_profile(profile: dict) -> dict:
    """Adapt a Kubernetes profile to the small planner input structure."""
    nodes = {}
    for name, node in sorted(profile["cluster"]["nodes"].items()):
        fabric = node.get("fabric_group")
        nodes[name] = {
            "name": name,
            "group": "distributed" if fabric else "standalone",
            "address": name,
            "accelerator": node["accelerator"],
            "gpus": 1 if node["accelerator"] == "gpu" else 0,
            "gpu_mem_gb": float(node["gpu_memory_gb"]),
            "fleet_role": "distributed" if fabric else "standalone",
            "vars": dict(node),
        }
    return {"nodes": nodes, "control": None}


def sanitize(name: str) -> str:
    """Model names may contain dots; container names may not."""
    return re.sub(r"[^a-z0-9-]", "-", name)


def eligible_placement_nodes(
    topology: dict, anchor_name: str, tensor_parallel: int
) -> list[str]:
    """Return every node on which this anchored placement may run."""
    if anchor_name not in topology["nodes"]:
        raise ValueError(f"unknown placement node {anchor_name!r}")
    if tensor_parallel == 1:
        return [anchor_name]
    anchor = topology["nodes"][anchor_name]
    fabric = anchor["vars"].get("fabric_group")
    candidates = sorted(
        name
        for name, item in topology["nodes"].items()
        if fabric and item["vars"].get("fabric_group") == fabric
    )
    if not fabric or len(candidates) < tensor_parallel:
        raise ValueError(
            f"tensor_parallel={tensor_parallel} on {anchor_name} requires a fabric group "
            "with enough nodes"
        )
    return candidates


def placement_nodes(topology: dict, anchor_name: str, tensor_parallel: int) -> list[str]:
    """Choose a deterministic subset for static capacity accounting."""
    candidates = eligible_placement_nodes(topology, anchor_name, tensor_parallel)
    if tensor_parallel == 1:
        return candidates
    anchor_index = candidates.index(anchor_name)
    return (candidates[anchor_index:] + candidates[:anchor_index])[:tensor_parallel]


def build_plan(models_doc: dict, topology: dict) -> dict:
    """Produce the fleet plan: per-node container lists and per-model upstream lists."""
    nodes = {
        name: {
            "address": info["address"],
            "accelerator": info["accelerator"],
            "gpus": info["gpus"],
            "gpu_mem_gb": info["gpu_mem_gb"],
            "fleet_role": info["fleet_role"],
            "group": info["group"],
            "containers": [],
        }
        for name, info in sorted(topology["nodes"].items())
    }
    models: dict[str, dict] = {}

    for model in sorted(models_doc.get("models") or [], key=lambda m: m["name"]):
        if model.get("state") != "active":
            continue
        local_path = f"/models/{model['name']}/{model['revision']}"
        entry = {
            "repo": model["repo"],
            "revision": model["revision"],
            "task": model.get("task", "generation"),
            "quant": model["quant"],
            "context_len": model["context_len"],
            "engine_args": dict(sorted((model.get("engine_args") or {}).items())),
            "owner": model["owner"],
            "approval_ticket": model["approval_ticket"],
            "local_path": local_path,
            "artifact_uri": model["artifact_uri"],
            "artifact_sha256": model["artifact_sha256"],
            "manifest_sha256": model["manifest_sha256"],
            "size_gb": model["size_gb"],
            "upstreams": [],
        }
        models[model["name"]] = entry

        for placement in sorted(model["placement"], key=lambda p: p["node"]):
            node_name = placement["node"]
            if node_name not in nodes:
                raise ValueError(
                    f"model {model['name']!r} placed on unknown node {node_name!r}"
                )
            tensor_parallel = int(placement.get("tensor_parallel", 1))
            eligible_nodes = eligible_placement_nodes(topology, node_name, tensor_parallel)
            for replica in range(int(placement.get("replicas", 1))):
                nodes[node_name]["containers"].append(
                    {
                        "model": model["name"],
                        "replica": replica,
                        "container_name": f"sf-{sanitize(model['name'])}-r{replica}",
                        "gpu_memory_utilization": placement["gpu_memory_utilization"],
                        "tensor_parallel": tensor_parallel,
                        "repo": model["repo"],
                        "revision": model["revision"],
                        "task": entry["task"],
                        "quant": model["quant"],
                        "context_len": model["context_len"],
                        "engine_args": entry["engine_args"],
                        "local_path": local_path,
                        "target_nodes": eligible_nodes,
                        "artifact_uri": model["artifact_uri"],
                        "artifact_sha256": model["artifact_sha256"],
                        "manifest_sha256": model["manifest_sha256"],
                        "size_gb": model["size_gb"],
                    }
                )

    # Ports: stable per node, ordered by (model, replica). Containers were appended in
    # sorted model order, so enumeration order is already deterministic.
    for node_name, node in nodes.items():
        node["containers"].sort(key=lambda c: (c["model"], c["replica"]))
        for idx, container in enumerate(node["containers"]):
            container["port"] = BASE_PORT + 1 + idx
            models[container["model"]]["upstreams"].append(
                {
                    "node": node_name,
                    "address": node["address"],
                    "port": container["port"],
                }
            )

    for entry in models.values():
        entry["upstreams"].sort(key=lambda u: (u["node"], u["port"]))

    return {"nodes": nodes, "models": models}


def plan_hash(plan: dict) -> str:
    """Hash the canonical plan for workload annotations and release evidence."""
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
