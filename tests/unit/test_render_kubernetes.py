"""Kubernetes rendering preserves policy across both DGX fabric profiles."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
import fleetplan  # noqa: E402
import render_kubernetes  # noqa: E402
from catalog_fixtures import active_catalog  # noqa: E402


def _render_as_amd64(tmp_path: Path, cluster: str):
    profile = yaml.safe_load(
        (REPO / "clusters" / cluster / "cluster.yaml").read_text(encoding="utf-8")
    )
    # Exercise the manifest topology independently from the deliberate ARM64
    # Ray image promotion gate.
    profile["cluster"]["architecture"] = "amd64"
    profile["cluster"]["model_storage"]["capacity_gb"] = 1000
    profile_path = tmp_path / f"{cluster}.yaml"
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    models_path = tmp_path / "models.yaml"
    models_path.write_text(
        yaml.safe_dump(active_catalog(fleetplan), sort_keys=False), encoding="utf-8"
    )
    output = tmp_path / cluster
    manifest = render_kubernetes.render_kubernetes(
        models_path,
        REPO / "teams.yaml",
        REPO / "versions.yml",
        profile_path,
        REPO / "gateway" / "auth.prod.yml.example",
        "prod",
        output,
    )
    return manifest, list(yaml.safe_load_all((output / "fleet.yaml").read_text()))


def _resources(docs, kind: str):
    return [doc for doc in docs if doc["kind"] == kind]


def test_direct_pair_limits_distributed_workers_to_the_selected_pair(tmp_path):
    manifest, docs = _render_as_amd64(tmp_path, "dgx-spark")
    assert manifest["gpu_sharing"] == {"mode": "exclusive", "replicas": 1}
    assert manifest["ray_services"] == 1
    ray = _resources(docs, "RayService")[0]
    worker = ray["spec"]["rayClusterConfig"]["workerGroupSpecs"][0]
    assert worker["replicas"] == 2
    assert worker["template"]["spec"]["nodeSelector"] == {
        "spark-fleet.example/fabric-group": "pair-b"
    }
    values = worker["template"]["spec"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"]
    assert values == ["spark-03", "spark-04"]
    container = worker["template"]["spec"]["containers"][0]
    environment = {item["name"]: item["value"] for item in container["env"]}
    assert environment["NCCL_SOCKET_IFNAME"] == "enp1s0f1np1"
    assert environment["NCCL_IB_HCA"] == "rocep1s0f1,roceP2p1s0f1"
    assert "memory" not in container["resources"]["limits"]
    head = ray["spec"]["rayClusterConfig"]["headGroupSpec"]["template"]["spec"]
    assert head["nodeSelector"] == {"kubernetes.io/hostname": "spark-03"}
    head_environment = {
        item["name"]: item["value"] for item in head["containers"][0]["env"]
    }
    assert head_environment["HF_HUB_OFFLINE"] == "1"
    assert head["containers"][0]["volumeMounts"][0]["readOnly"] is True


def test_switched_profile_exposes_all_eight_nodes_to_gang_scheduling(tmp_path):
    manifest, docs = _render_as_amd64(tmp_path, "dgx-spark-switched")
    assert manifest["ray_services"] == 1
    ray = _resources(docs, "RayService")[0]
    worker = ray["spec"]["rayClusterConfig"]["workerGroupSpecs"][0]
    values = worker["template"]["spec"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"]
    assert values == [f"spark-{number:02d}" for number in range(1, 9)]
    assert worker["replicas"] == 2
    jobs = _resources(docs, "Job")
    assert len(jobs) == 9
    assert {
        job["spec"]["template"]["spec"]["nodeSelector"]["kubernetes.io/hostname"]
        for job in jobs
        if job["metadata"]["labels"]["spark-fleet.example/model"] == "qwen3-14b-awq"
    } == {f"spark-{number:02d}" for number in range(1, 9)}


def test_gateway_remains_openai_compatible_and_internal(tmp_path):
    _, docs = _render_as_amd64(tmp_path, "dgx-spark")
    service = next(
        item for item in _resources(docs, "Service")
        if item["metadata"]["name"] == "spark-fleet-gateway"
    )
    assert service["spec"]["type"] == "ClusterIP"
    rules = next(
        item for item in _resources(docs, "ConfigMap")
        if item["metadata"]["name"] == "spark-fleet-gateway"
    )["data"]["apisix.yaml"]
    assert "/v1/chat/completions" in rules
    assert "/v1/embeddings" in rules
    assert "qwen3-14b-awq" in rules
    assert "bge-m3" in rules
    model_deployment = next(
        item for item in _resources(docs, "Deployment")
        if item["metadata"]["labels"].get("spark-fleet.example/model") == "bge-m3"
    )
    container = model_deployment["spec"]["template"]["spec"]["containers"][0]
    environment = {item["name"]: item["value"] for item in container["env"]}
    assert environment["PYTORCH_CUDA_ALLOC_CONF"] == "expandable_segments:True"
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"
    assert container["args"][:2] == [
        "--model",
        "/models/bge-m3/5617a9f61b028005a4858fdac845db406aefb181",
    ]
    assert "--revision" not in container["args"]
    assert all(
        mount.get("readOnly")
        for mount in container["volumeMounts"]
        if mount["name"] == "model-cache"
    )
    assert "memory" not in container["resources"]["limits"]


def test_active_models_render_verified_node_staging_jobs(tmp_path):
    _, docs = _render_as_amd64(tmp_path, "dgx-spark")
    jobs = _resources(docs, "Job")
    assert len(jobs) == 4
    assert {job["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] for job in jobs} == {"-1"}
    assert {
        job["spec"]["template"]["spec"]["nodeSelector"]["kubernetes.io/hostname"]
        for job in jobs
    } == {"spark-01", "spark-02", "spark-03", "spark-04"}
    assert all(
        "MODEL_STORE_BEARER_TOKEN" not in str(job)
        for job in jobs
    )


def test_active_models_cannot_render_before_disk_capacity_is_recorded(tmp_path):
    models_path = tmp_path / "models.yaml"
    models_path.write_text(
        yaml.safe_dump(active_catalog(fleetplan), sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="model_storage.capacity_gb"):
        render_kubernetes.render_kubernetes(
            models_path,
            REPO / "teams.yaml",
            REPO / "versions.yml",
            REPO / "clusters" / "dgx-spark" / "cluster.yaml",
            REPO / "gateway" / "auth.prod.yml.example",
            "prod",
            tmp_path / "blocked",
        )


@pytest.mark.parametrize("cluster", ["dgx-spark", "dgx-spark-switched"])
def test_dgx_profiles_require_a_pinned_arm64_ray_image(tmp_path, cluster):
    models_path = tmp_path / "models.yaml"
    models_path.write_text(
        yaml.safe_dump(active_catalog(fleetplan), sort_keys=False), encoding="utf-8"
    )
    profile = yaml.safe_load(
        (REPO / "clusters" / cluster / "cluster.yaml").read_text(encoding="utf-8")
    )
    profile["cluster"]["model_storage"]["capacity_gb"] = 1000
    profile_path = tmp_path / f"{cluster}-arm64.yaml"
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="arm64 Ray Serve LLM image"):
        render_kubernetes.render_kubernetes(
            models_path,
            REPO / "teams.yaml",
            REPO / "versions.yml",
            profile_path,
            REPO / "gateway" / "auth.prod.yml.example",
            "prod",
            tmp_path / cluster,
        )
