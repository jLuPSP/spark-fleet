"""scripts/validate.py must reject broken catalogs with useful errors."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
VALIDATE = REPO / "scripts" / "validate.py"
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
import fleetplan  # noqa: E402
from catalog_fixtures import active_catalog  # noqa: E402


def _base_models() -> dict:
    return active_catalog(fleetplan)


def _base_teams() -> dict:
    return yaml.safe_load((REPO / "teams.yaml").read_text(encoding="utf-8"))


def _run(
    tmp_path: Path,
    models: dict,
    teams: dict,
    extra_args=(),
    storage_capacity=1000,
) -> tuple[int, str]:
    models_file = tmp_path / "models.yaml"
    teams_file = tmp_path / "teams.yaml"
    models_file.write_text(yaml.safe_dump(models), encoding="utf-8")
    teams_file.write_text(yaml.safe_dump(teams), encoding="utf-8")
    profile = yaml.safe_load(
        (REPO / "clusters" / "dgx-spark" / "cluster.yaml").read_text(encoding="utf-8")
    )
    profile["cluster"]["model_storage"]["capacity_gb"] = storage_capacity
    profile_file = tmp_path / "cluster.yaml"
    profile_file.write_text(yaml.safe_dump(profile), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATE),
            "--models", str(models_file),
            "--teams", str(teams_file),
            "--versions", str(REPO / "versions.yml"),
            "--profile", str(profile_file),
            *extra_args,
        ],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_repo_catalog_is_valid(tmp_path):
    code, out = _run(tmp_path, _base_models(), _base_teams(), ["--require-digests"])
    assert code == 0, out


def test_duplicate_model_name_rejected(tmp_path):
    models = _base_models()
    models["models"].append(dict(models["models"][0]))
    code, out = _run(tmp_path, models, _base_teams())
    assert code == 1
    assert "duplicate model name" in out


def test_unknown_model_in_team_rejected(tmp_path):
    teams = _base_teams()
    teams["teams"][0]["allowed_models"].append("ghost-model")
    code, out = _run(tmp_path, _base_models(), teams)
    assert code == 1
    assert "ghost-model" in out and "not in the model catalog" in out


def test_unknown_node_rejected(tmp_path):
    models = _base_models()
    models["models"][0]["placement"][0]["node"] = "node99"
    code, out = _run(tmp_path, models, _base_teams())
    assert code == 1
    assert "node99" in out


def test_oversubscribed_node_rejected(tmp_path):
    models = _base_models()
    for model in models["models"]:
        model["placement"][0].update(node="node1", gpu_memory_utilization=0.9)
    code, out = _run(tmp_path, models, _base_teams())
    assert code == 1
    assert "oversubscribed" in out


def test_tensor_parallel_larger_than_a_physical_pair_is_rejected(tmp_path):
    models = _base_models()
    models["models"][0]["placement"][0]["tensor_parallel"] = 3
    code, out = _run(tmp_path, models, _base_teams())
    assert code == 1
    assert "fabric group" in out


def test_dgx_profile_encodes_four_real_pairs_and_the_normal_75_25_preference():
    profile = yaml.safe_load(
        (REPO / "clusters" / "dgx-spark" / "cluster.yaml").read_text()
    )
    nodes = profile["cluster"]["nodes"]
    assert profile["cluster"]["fabric_topology"] == "direct-pairs"
    pairs: dict[str, list[dict]] = {}
    for node in nodes.values():
        pairs.setdefault(node["fabric_group"], []).append(node)

    assert len(nodes) == 8
    assert sorted(len(members) for members in pairs.values()) == [2, 2, 2, 2]
    assert sum(node.get("preferred_workload") == "inference" for node in nodes.values()) == 6
    assert sum(node.get("preferred_workload") == "training" for node in nodes.values()) == 2
    assert [node["kubernetes_node"] for node in nodes.values() if node.get("control_plane")] == ["spark-01"]


def test_switched_profile_encodes_one_eight_node_fabric_and_75_25_preference():
    profile = yaml.safe_load(
        (REPO / "clusters" / "dgx-spark-switched" / "cluster.yaml").read_text()
    )
    nodes = profile["cluster"]["nodes"]
    assert profile["cluster"]["fabric_topology"] == "switched"
    assert len(nodes) == 8
    assert {node["fabric_group"] for node in nodes.values()} == {"fabric-a"}
    assert sum(node["preferred_workload"] == "inference" for node in nodes.values()) == 6
    assert sum(node["preferred_workload"] == "training" for node in nodes.values()) == 2


def test_unpinned_revision_rejected(tmp_path):
    models = _base_models()
    models["models"][0]["revision"] = "main"
    code, out = _run(tmp_path, models, _base_teams())
    assert code == 1
    assert "revision" in out


def test_orphan_model_only_warns(tmp_path):
    teams = _base_teams()
    for team in teams["teams"]:
        team["allowed_models"] = [m for m in team["allowed_models"] if m != "bge-m3"]
    code, out = _run(tmp_path, _base_models(), teams)
    assert code == 0
    assert "WARN" in out and "bge-m3" in out


def test_approved_model_is_valid_but_not_planned(tmp_path):
    models = _base_models()
    models["models"][0]["state"] = "approved"
    for field in ("artifact_uri", "artifact_sha256", "manifest_sha256", "size_gb"):
        models["models"][0].pop(field)
    code, out = _run(tmp_path, models, _base_teams())
    assert code == 0, out
    assert "approved but not active" in out


def test_active_model_requires_internal_artifact_metadata(tmp_path):
    models = _base_models()
    models["models"][0].pop("artifact_sha256")
    code, out = _run(tmp_path, models, _base_teams())
    assert code == 1
    assert "artifact_sha256" in out


def test_active_model_must_use_approved_internal_store(tmp_path):
    models = _base_models()
    models["models"][0]["artifact_uri"] = "https://huggingface.co/example/model.tar"
    code, out = _run(tmp_path, models, _base_teams())
    assert code == 1
    assert "outside model-store-policy.yml" in out


def test_active_model_requires_recorded_disk_capacity(tmp_path):
    code, out = _run(
        tmp_path, _base_models(), _base_teams(), storage_capacity=None
    )
    assert code == 1
    assert "capacity_gb is null" in out


def test_model_cache_and_staging_headroom_are_enforced(tmp_path):
    models = _base_models()
    for model in models["models"]:
        model["size_gb"] = 600
    code, out = _run(tmp_path, models, _base_teams())
    assert code == 1
    assert "model storage" in out and "staging archive" in out
