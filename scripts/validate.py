"""Validate catalogs, policy, pins, and placement against a cluster profile."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import jsonschema
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleetplan  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
CAPACITY_HEADROOM = 0.95  # fraction of a node's GPUs the catalog may claim


def _load(path: Path, errors: list[str]):
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError:
        errors.append(f"{path}: file not found")
    except yaml.YAMLError as exc:
        errors.append(f"{path}: YAML parse error: {exc}")
    return None


def _schema_check(doc, schema_path: Path, doc_name: str, errors: list[str]) -> bool:
    import json

    with open(schema_path, encoding="utf-8") as fh:
        schema = json.load(fh)
    validator = jsonschema.Draft202012Validator(schema)
    found = False
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{doc_name}: schema violation at {where}: {err.message}")
        found = True
    return not found


def check_models(models_doc, profile, model_store_policy, errors, warnings):
    models = models_doc.get("models") or []
    cluster = profile["cluster"]
    nodes = cluster["nodes"]

    names = [m["name"] for m in models]
    for name in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"model catalog: duplicate model name {name!r}")

    claims: dict[str, list[tuple[str, float]]] = {}
    storage_claims: dict[str, dict[str, float]] = {}
    allowed_prefixes = tuple(model_store_policy["allowed_artifact_prefixes"])
    for model in models:
        if model.get("state") != "active":
            continue
        if not model["artifact_uri"].startswith(allowed_prefixes):
            errors.append(
                f"model catalog: {model['name']} artifact_uri is outside "
                "model-store-policy.yml"
            )
        seen_nodes = []
        for pl in model.get("placement") or []:
            node_name = pl.get("node")
            if node_name in seen_nodes:
                errors.append(
                    f"model catalog: {model['name']} lists node {node_name!r} twice; "
                    "raise replicas instead"
                )
            seen_nodes.append(node_name)
            node = nodes.get(node_name)
            if node is None:
                errors.append(
                    f"model catalog: {model['name']} placed on {node_name!r}, "
                    "which is not in the cluster profile"
                )
                continue

            tp = int(pl.get("tensor_parallel", 1))
            replicas = int(pl.get("replicas", 1))
            selected_nodes = [node]
            eligible_nodes = [node]
            if tp > 1:
                fabric = node.get("fabric_group")
                candidates = sorted(
                    (
                        candidate
                        for candidate in nodes.values()
                        if fabric and candidate.get("fabric_group") == fabric
                    ),
                    key=lambda candidate: candidate["kubernetes_node"],
                )
                # The placement's anchor is the preferred first node. Rotate the
                # shared-fabric candidates around it for deterministic capacity
                # accounting while Kubernetes retains the full eligible pool.
                anchor_index = candidates.index(node) if node in candidates else 0
                selected_nodes = (candidates[anchor_index:] + candidates[:anchor_index])[:tp]
                eligible_nodes = candidates
                if not fabric or len(selected_nodes) < tp:
                    errors.append(
                        f"model catalog: {model['name']} tensor_parallel={tp} on {node_name} "
                        "requires a fabric group with enough nodes"
                    )
                    continue

            for eligible in eligible_nodes:
                storage_claims.setdefault(eligible["kubernetes_node"], {})[
                    model["name"]
                ] = float(model["size_gb"])

            memory_values = [item["gpu_memory_gb"] for item in selected_nodes if item["gpu_memory_gb"]]
            budget = pl["gpu_memory_utilization"] * (min(memory_values) if memory_values else 0) * tp
            if budget and model["vram_gb"] > budget:
                errors.append(
                    f"model catalog: {model['name']} on {node_name}: vram_gb="
                    f"{model['vram_gb']} exceeds gpu_memory_utilization*gpu_memory_gb*tp="
                    f"{budget:.1f}"
                )
            exclusive = cluster["gpu_sharing"]["mode"] == "exclusive"
            claim = float(replicas) if exclusive else pl["gpu_memory_utilization"] * replicas
            for selected in selected_nodes:
                physical = selected["kubernetes_node"]
                claims.setdefault(physical, []).append((model["name"], claim))

    # Exclusive mode admits one GPU pod; time-sliced claims share one memory pool.
    for physical_node, entries in sorted(claims.items()):
        total = sum(c for _, c in entries)
        limit = 1.0 if cluster["gpu_sharing"]["mode"] == "exclusive" else CAPACITY_HEADROOM
        if total > limit:
            detail = ", ".join(f"{m}={c:.2f}" for m, c in entries)
            errors.append(
                f"capacity: {physical_node} oversubscribed: claims {total:.2f} "
                f"GPU claims ({detail}) > {limit:.2f}"
            )

    if storage_claims:
        storage = cluster["model_storage"]
        capacity = storage["capacity_gb"]
        if capacity is None:
            errors.append(
                "model storage: capacity_gb is null; record the purchased NVMe SKU "
                "before activating a model"
            )
        else:
            steady_limit = capacity * storage["max_cache_utilization"]
            staging_limit = capacity - storage["minimum_free_gb"]
            for physical_node, artifacts in sorted(storage_claims.items()):
                steady = sum(artifacts.values())
                largest = max(artifacts.values())
                if steady > steady_limit:
                    errors.append(
                        f"model storage: {physical_node} desired cache {steady:.1f} GB "
                        f"exceeds {steady_limit:.1f} GB utilization budget"
                    )
                if steady + largest > staging_limit:
                    errors.append(
                        f"model storage: {physical_node} needs {steady + largest:.1f} GB "
                        "for desired models plus largest staging archive, leaving less "
                        f"than minimum_free_gb={storage['minimum_free_gb']}"
                    )


def check_teams(teams_doc, models_doc, errors, warnings):
    teams = teams_doc.get("teams") or []
    model_names = {m["name"] for m in (models_doc.get("models") or [])}
    active_names = {
        m["name"] for m in (models_doc.get("models") or []) if m.get("state") == "active"
    }

    for field, label in (("name", "team name"), ("entra_app_role", "app role")):
        values = [t[field] for t in teams]
        for value in sorted({v for v in values if values.count(v) > 1}):
            errors.append(f"teams.yaml: duplicate {label} {value!r}")

    routable = set()
    for team in teams:
        for model in team.get("allowed_models") or []:
            if model not in model_names:
                errors.append(
                    f"teams.yaml: {team['name']} allows {model!r}, "
                    "which is not in the model catalog"
                )
            routable.add(model)

    for inactive in sorted(routable - active_names):
        warnings.append(
            f"model catalog: {inactive} is approved but not active; "
            "it will not appear in /v1/models"
        )

    for orphan in sorted(model_names - routable):
        warnings.append(
            f"model catalog: {orphan} has no team entitlement"
        )


def check_versions(versions_doc, require_digests, errors, warnings):
    images = versions_doc.get("images") or {}
    for key, image in sorted(images.items()):
        if not isinstance(image, dict):
            continue
        if image.get("digest") is None:
            msg = f"versions.yml: images.{key} has no pinned digest (run scripts/pin_digests.py)"
            (errors if require_digests else warnings).append(msg)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default=fleetplan.MODEL_CATALOG, type=Path)
    parser.add_argument("--teams", default=REPO / "teams.yaml", type=Path)
    parser.add_argument(
        "--model-store-policy", default=REPO / "model-store-policy.yml", type=Path
    )
    parser.add_argument("--versions", default=REPO / "versions.yml", type=Path)
    parser.add_argument(
        "--profile", default=REPO / "clusters" / "dgx-spark" / "cluster.yaml", type=Path
    )
    parser.add_argument("--require-digests", action="store_true")
    args = parser.parse_args()

    errors: list[str] = []
    warnings: list[str] = []

    try:
        models_doc = fleetplan.load_models(args.models)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        errors.append(f"model catalog: {exc}")
        models_doc = None
    teams_doc = _load(args.teams, errors)
    model_store_policy = _load(args.model_store_policy, errors)
    versions_doc = _load(args.versions, errors)
    profile = _load(args.profile, errors)
    if errors:
        for err in errors:
            print(f"ERROR: {err}")
        return 1

    schemas = REPO / "schemas"
    models_ok = _schema_check(models_doc, schemas / "models.schema.json", "model catalog", errors)
    teams_ok = _schema_check(teams_doc, schemas / "teams.schema.json", "teams.yaml", errors)
    versions_ok = _schema_check(versions_doc, schemas / "versions.schema.json", "versions.yml", errors)
    profile_ok = _schema_check(profile, schemas / "cluster.schema.json", "cluster profile", errors)
    model_store_policy_ok = _schema_check(
        model_store_policy,
        schemas / "model-store-policy.schema.json",
        "model-store-policy.yml",
        errors,
    )

    if models_ok and profile_ok and model_store_policy_ok:
        check_models(models_doc, profile, model_store_policy, errors, warnings)
    if models_ok and teams_ok:
        check_teams(teams_doc, models_doc, errors, warnings)
    if versions_ok:
        check_versions(versions_doc, args.require_digests, errors, warnings)

    # The plan must build; this catches anything the individual checks missed.
    if models_ok and profile_ok and not errors:
        try:
            plan = fleetplan.build_plan(models_doc, fleetplan.topology_from_profile(profile))
            for node_name, node in plan["nodes"].items():
                if len(node["containers"]) > fleetplan.MAX_CONTAINERS_PER_NODE:
                    errors.append(f"plan: {node_name} exceeds the container/port budget")
        except ValueError as exc:
            errors.append(f"plan: {exc}")

    for warning in warnings:
        print(f"WARN: {warning}")
    for err in errors:
        print(f"ERROR: {err}")
    if errors:
        print(f"\nvalidate: FAILED ({len(errors)} error(s), {len(warnings)} warning(s))")
        return 1
    print(f"validate: OK ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
