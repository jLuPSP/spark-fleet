"""Render vLLM, KubeRay, APISIX, and test fixtures from one fleet catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import jsonschema
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "gateway"))
import fleetplan  # noqa: E402
import generate_apisix  # noqa: E402

SERVE_PORT = 8000
MODEL_HOST_ROOT = "/var/lib/spark-fleet/models"
MODEL_MOUNT_ROOT = "/models"


def image_ref(versions: dict, key: str) -> str:
    image = versions["images"][key]
    if not image.get("digest"):
        raise ValueError(f"versions.yml images.{key}.digest is not pinned")
    return f"{image['repo']}@{image['digest']}"


def slug(value: str) -> str:
    return fleetplan.sanitize(value).strip("-")[:63]


def labels(cluster_name: str, model: str | None = None, node: str | None = None) -> dict:
    result = {
        "app.kubernetes.io/part-of": "spark-fleet",
        "spark-fleet.example/cluster": cluster_name,
    }
    if model:
        result["spark-fleet.example/model"] = model
    if node:
        result["spark-fleet.example/logical-node"] = node
    return result


def _validate_profile(profile: dict) -> None:
    schema = json.loads((REPO / "schemas" / "cluster.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(profile)
    cluster = profile["cluster"]
    gateway = cluster["gateway"]
    if gateway["service_type"] == "NodePort" and "node_port" not in gateway:
        raise ValueError("cluster.gateway.node_port is required for NodePort")
    if cluster["gpu_sharing"]["mode"] == "exclusive" and cluster["gpu_sharing"]["replicas"] != 1:
        raise ValueError("exclusive GPU sharing requires replicas: 1")
    for name, node in cluster["nodes"].items():
        if node["accelerator"] == "gpu" and not node.get("gpu_resource"):
            raise ValueError(f"cluster node {name}: gpu_resource is required for GPU nodes")
        expected = "mock" if node["simulated"] else "gpu"
        if node["accelerator"] != expected:
            raise ValueError(
                f"cluster node {name}: simulated={node['simulated']} requires accelerator={expected}"
            )


def resolve_plan(models_doc: dict, profile: dict) -> tuple[dict, dict[tuple[str, int], str]]:
    """Resolve logical placements to stable Kubernetes Service addresses."""
    plan = fleetplan.build_plan(models_doc, fleetplan.topology_from_profile(profile))
    services: dict[tuple[str, int], str] = {}
    pair_backend = profile["cluster"]["pair_backend"]
    for node_name, node in plan["nodes"].items():
        for container in node["containers"]:
            name = slug(f"sf-{container['model']}-{node_name}-r{container['replica']}")
            if container["tensor_parallel"] > 1 and pair_backend == "kuberay":
                service = f"{name}-serve-svc"
            else:
                service = name
            services[(node_name, container["port"])] = service

    namespace = profile["cluster"]["namespace"]
    for model in plan["models"].values():
        for upstream in model["upstreams"]:
            service = services[(upstream["node"], upstream["port"])]
            upstream["address"] = f"{service}.{namespace}.svc.cluster.local"
            upstream["port"] = SERVE_PORT
    return plan, services


def _engine_cli(container: dict) -> list[str]:
    args = [
        "--model", container["local_path"],
        "--served-model-name", container["model"],
        "--max-model-len", str(container["context_len"]),
        "--port", str(SERVE_PORT),
        "--tensor-parallel-size", str(container["tensor_parallel"]),
        "--gpu-memory-utilization", str(container["gpu_memory_utilization"]),
    ]
    for key, value in sorted(container["engine_args"].items()):
        flag = f"--{key.replace('_', '-')}"
        if value is True:
            args.append(flag)
        elif value is not False:
            args.extend([flag, str(value)])
    return args


def _cache_volume() -> tuple[list[dict], list[dict]]:
    return (
        [{"name": "model-cache", "hostPath": {"path": MODEL_HOST_ROOT, "type": "Directory"}},
         {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "4Gi"}}],
        [{"name": "model-cache", "mountPath": MODEL_MOUNT_ROOT, "readOnly": True},
         {"name": "shm", "mountPath": "/dev/shm"}],
    )


def offline_model_env() -> list[dict]:
    return [
        {"name": "HF_HOME", "value": MODEL_MOUNT_ROOT},
        {"name": "HF_HUB_OFFLINE", "value": "1"},
        {"name": "TRANSFORMERS_OFFLINE", "value": "1"},
        {"name": "HF_DATASETS_OFFLINE", "value": "1"},
    ]


def standalone_resources(
    cluster: dict, versions: dict, node_name: str, node: dict, container: dict, service: str
) -> list[dict]:
    namespace = cluster["namespace"]
    workload_labels = labels(cluster["name"], container["model"], node_name)
    workload_labels["app.kubernetes.io/name"] = service
    volumes, mounts = _cache_volume()

    if node["simulated"]:
        image = image_ref(versions, "python")
        command = ["python", "/app/server.py"]
        env = [
            {"name": "NODE_NAME", "value": node_name},
            {"name": "MOCK_LISTENERS", "value": json.dumps([{"model": container["model"], "port": SERVE_PORT}])},
        ]
        resources = {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}}
        pod_volumes = [{"name": "mock-server", "configMap": {"name": "spark-fleet-mock"}}]
        pod_mounts = [{"name": "mock-server", "mountPath": "/app/server.py", "subPath": "server.py", "readOnly": True}]
        runtime_class = None
        image_pull_policy = "IfNotPresent"
    else:
        image = image_ref(versions, "vllm")
        command = _engine_cli(container)
        env = offline_model_env() + [
            {"name": "PYTORCH_CUDA_ALLOC_CONF", "value": "expandable_segments:True"},
        ]
        shared_gpu = (
            node["accelerator"] == "gpu"
            and cluster["gpu_sharing"]["mode"] == "time-slicing"
        )
        requests = {"cpu": "1500m" if shared_gpu else "2", "memory": "8Gi"}
        # DGX Spark uses unified CPU/GPU memory. A low container memory limit can
        # kill a model that otherwise fits, so kubelet system reservation protects
        # the host while the exclusive GPU claim prevents competing model pods.
        limits = {"cpu": "4" if shared_gpu else "6"}
        if node["accelerator"] == "gpu":
            requests[node["gpu_resource"]] = 1
            limits[node["gpu_resource"]] = 1
        resources = {"requests": requests, "limits": limits}
        pod_volumes, pod_mounts = volumes, mounts
        runtime_class = "nvidia" if node["accelerator"] == "gpu" else None
        image_pull_policy = "IfNotPresent"

    app_container = {
        "name": "server",
        "image": image,
        "imagePullPolicy": image_pull_policy,
        "ports": [{"name": "http", "containerPort": SERVE_PORT}],
        "env": env,
        "resources": resources,
        # First load and torch compilation can take several minutes even with a
        # verified local artifact. A startup probe gates liveness until ready.
        "startupProbe": {"httpGet": {"path": "/health", "port": "http"}, "periodSeconds": 10, "failureThreshold": 90},
        "readinessProbe": {"httpGet": {"path": "/health", "port": "http"}, "periodSeconds": 10, "failureThreshold": 30},
        "livenessProbe": {"httpGet": {"path": "/health", "port": "http"}, "periodSeconds": 30, "failureThreshold": 5},
    }
    if command:
        app_container["command" if node["simulated"] else "args"] = command
    if pod_mounts:
        app_container["volumeMounts"] = pod_mounts

    pod_spec = {
        "nodeSelector": {"kubernetes.io/hostname": node["kubernetes_node"]},
        "containers": [app_container],
        "volumes": pod_volumes,
        "terminationGracePeriodSeconds": 30,
    }
    if runtime_class:
        pod_spec["runtimeClassName"] = runtime_class
        pod_spec["tolerations"] = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]

    return [
        {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {
                "name": service, "namespace": namespace, "labels": workload_labels,
                "annotations": {"argocd.argoproj.io/sync-wave": "0"},
            },
            "spec": {
                "replicas": 1,
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": {"app.kubernetes.io/name": service}},
                "template": {
                    "metadata": {"labels": workload_labels},
                    "spec": pod_spec,
                },
            },
        },
        {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": service, "namespace": namespace, "labels": workload_labels},
            "spec": {
                "selector": {"app.kubernetes.io/name": service},
                "ports": [{"name": "http", "port": SERVE_PORT, "targetPort": "http"}],
            },
        },
    ]


def ray_service_resource(
    cluster: dict, versions: dict, node_name: str, node: dict, container: dict, name: str
) -> dict:
    if cluster["architecture"] != "amd64":
        raise ValueError(
            f"{cluster['name']}: official ray-llm {versions['platform']['ray']} image is amd64-only; "
            "an arm64 Ray Serve LLM image must be pinned before rendering DGX Spark"
        )
    tp = container["tensor_parallel"]
    fabric = node.get("fabric_group")
    worker_nodes = sorted(
        item["kubernetes_node"] for item in cluster["nodes"].values()
        if fabric and item.get("fabric_group") == fabric and not item["simulated"]
    )
    if len(worker_nodes) < tp:
        raise ValueError(f"{name}: tensor_parallel={tp} needs {tp} real nodes in fabric group {fabric!r}")

    engine_kwargs = {
        "dtype": container["engine_args"].get("dtype", "auto"),
        "max_model_len": container["context_len"],
        "device": "auto",
        "gpu_memory_utilization": container["gpu_memory_utilization"],
        "tensor_parallel_size": tp,
    }
    for key, value in container["engine_args"].items():
        if key not in {"runner", "dtype"}:
            engine_kwargs[key] = value
    serve_config = {
        "applications": [{
            "name": "llms",
            "import_path": "ray.serve.llm:build_openai_app",
            "route_prefix": "/",
            "args": {"llm_configs": [{
                "model_loading_config": {
                    "model_id": container["model"],
                    "model_source": container["local_path"],
                },
                "engine_kwargs": engine_kwargs,
                "deployment_config": {"num_replicas": 1, "max_ongoing_requests": 32},
            }]},
        }]
    }
    ray_image = image_ref(versions, "ray_llm")
    worker_labels = labels(cluster["name"], container["model"], node_name)
    worker_labels["spark-fleet.example/ray-worker-group"] = name
    cache_volume = {"name": "model-cache", "hostPath": {"path": MODEL_HOST_ROOT, "type": "Directory"}}
    cache_mount = {"name": "model-cache", "mountPath": MODEL_MOUNT_ROOT, "readOnly": True}
    return {
        "apiVersion": "ray.io/v1", "kind": "RayService",
        "metadata": {
            "name": name,
            "namespace": cluster["namespace"],
            "labels": labels(cluster["name"], container["model"], node_name),
            "annotations": {"argocd.argoproj.io/sync-wave": "0"},
        },
        "spec": {
            "serveConfigV2": yaml.safe_dump(serve_config, sort_keys=False),
            "rayClusterConfig": {
                "rayVersion": versions["platform"]["ray"],
                "headGroupSpec": {
                    "rayStartParams": {"num-cpus": "0", "num-gpus": "0", "dashboard-host": "0.0.0.0"},
                    "template": {"spec": {
                        "nodeSelector": {"kubernetes.io/hostname": node["kubernetes_node"]},
                        "tolerations": [{
                            "key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"
                        }],
                        "volumes": [cache_volume],
                        "containers": [{
                            "name": "ray-head", "image": ray_image,
                            "ports": [
                                {"containerPort": SERVE_PORT, "name": "serve"},
                                {"containerPort": 8265, "name": "dashboard"},
                                {"containerPort": 6379, "name": "gcs"},
                                {"containerPort": 10001, "name": "client"},
                            ],
                            "env": offline_model_env(),
                            "volumeMounts": [cache_mount],
                            "resources": {"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {"cpu": "2", "memory": "5Gi"}},
                        }],
                    }}
                },
                "workerGroupSpecs": [{
                    "groupName": "gpu-pair", "replicas": tp, "minReplicas": tp, "maxReplicas": tp,
                    "numOfHosts": 1, "rayStartParams": {"num-gpus": "1"},
                    "template": {
                        "metadata": {"labels": worker_labels},
                        "spec": {
                            "runtimeClassName": "nvidia",
                            "nodeSelector": {"spark-fleet.example/fabric-group": fabric},
                            "affinity": {
                                "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": worker_nodes}]}]}},
                                "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"labelSelector": {"matchLabels": {"spark-fleet.example/ray-worker-group": name}}, "topologyKey": "kubernetes.io/hostname"}]},
                            },
                            "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
                            "volumes": [cache_volume],
                            "containers": [{
                                "name": "ray-worker", "image": ray_image,
                                "env": offline_model_env() + [
                                    {"name": "PYTORCH_CUDA_ALLOC_CONF", "value": "expandable_segments:True"},
                                    {"name": "NCCL_SOCKET_IFNAME", "value": cluster["fabric"]["socket_interface"]},
                                    {"name": "NCCL_IB_HCA", "value": ",".join(cluster["fabric"]["rdma_hcas"])},
                                    {"name": "NCCL_DEBUG", "value": "WARN"},
                                ],
                                "volumeMounts": [cache_mount],
                                "resources": {
                                    "requests": {"cpu": "2", "memory": "8Gi", node["gpu_resource"]: 1},
                                    "limits": {"cpu": "6", node["gpu_resource"]: 1},
                                },
                            }],
                        },
                    },
                }],
            },
        },
    }


def model_staging_resources(cluster: dict, versions: dict, plan: dict) -> list[dict]:
    """Render one idempotent Argo staging job per model and physical node."""
    stages: dict[tuple[str, str], dict] = {}
    for planned_node in plan["nodes"].values():
        for container in planned_node["containers"]:
            for target_node in container["target_nodes"]:
                stages[(container["model"], target_node)] = container
    if not stages:
        return []

    namespace = cluster["namespace"]
    config_name = "spark-fleet-model-stager"
    resources: list[dict] = [{
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": config_name,
            "namespace": namespace,
            "annotations": {"argocd.argoproj.io/sync-wave": "-2"},
        },
        "data": {"stage_model.py": (REPO / "scripts" / "stage_model.py").read_text()},
    }]
    for (model_name, logical_node), container in sorted(stages.items()):
        node = cluster["nodes"][logical_node]
        if node["simulated"]:
            continue
        name = slug(f"sf-stage-{model_name}-{container['revision'][:8]}-{logical_node}")
        resources.append({
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels(cluster["name"], model_name, logical_node),
                "annotations": {"argocd.argoproj.io/sync-wave": "-1"},
            },
            "spec": {
                "backoffLimit": 2,
                "template": {
                    "metadata": {"labels": labels(cluster["name"], model_name, logical_node)},
                    "spec": {
                        "restartPolicy": "Never",
                        "nodeSelector": {"kubernetes.io/hostname": node["kubernetes_node"]},
                        "tolerations": [{
                            "key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"
                        }],
                        "volumes": [
                            {"name": "models", "hostPath": {"path": MODEL_HOST_ROOT, "type": "DirectoryOrCreate"}},
                            {"name": "stager", "configMap": {"name": config_name}},
                        ],
                        "containers": [{
                            "name": "stage",
                            "image": image_ref(versions, "python"),
                            "command": ["python", "/opt/spark-fleet/stage_model.py"],
                            "args": [
                                "--artifact-uri", container["artifact_uri"],
                                "--artifact-sha256", container["artifact_sha256"],
                                "--manifest-sha256", container["manifest_sha256"],
                                "--size-gb", str(container["size_gb"]),
                                "--minimum-free-gb", str(cluster["model_storage"]["minimum_free_gb"]),
                                "--root", MODEL_HOST_ROOT,
                                "--model", model_name,
                                "--revision", container["revision"],
                            ],
                            "envFrom": [{
                                "secretRef": {"name": "spark-fleet-model-store", "optional": True}
                            }],
                            "volumeMounts": [
                                {"name": "models", "mountPath": MODEL_HOST_ROOT},
                                {"name": "stager", "mountPath": "/opt/spark-fleet", "readOnly": True},
                            ],
                            "resources": {
                                "requests": {"cpu": "500m", "memory": "512Mi"},
                                "limits": {"cpu": "2", "memory": "2Gi"},
                            },
                        }],
                    },
                },
            },
        })
    return resources


def gateway_resources(
    cluster: dict, versions: dict, rules: str, auth_mode: str
) -> list[dict]:
    namespace = cluster["namespace"]
    gateway_name = "spark-fleet-gateway"
    gateway_labels = labels(cluster["name"])
    gateway_labels["app.kubernetes.io/name"] = gateway_name
    config = (REPO / "gateway" / f"config.{auth_mode}.yaml").read_text()
    config_digest = hashlib.sha256((config + "\0" + rules).encode()).hexdigest()
    service_spec: dict = {
        "type": cluster["gateway"]["service_type"],
        "selector": {"app.kubernetes.io/name": gateway_name},
        "ports": [{"name": "http", "port": 80, "targetPort": "http"}],
    }
    if service_spec["type"] == "NodePort":
        service_spec["ports"][0]["nodePort"] = cluster["gateway"]["node_port"]
    return [
        {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": gateway_name, "namespace": namespace},
            "data": {"config.yaml": config, "apisix.yaml": rules},
        },
        {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": gateway_name, "namespace": namespace, "labels": gateway_labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app.kubernetes.io/name": gateway_name}},
                "template": {
                    "metadata": {
                        "labels": gateway_labels,
                        "annotations": {"spark-fleet.example/config-sha256": config_digest},
                    },
                    "spec": {"containers": [{
                        "name": "apisix", "image": image_ref(versions, "apisix"),
                        "ports": [{"name": "http", "containerPort": 9080}, {"name": "metrics", "containerPort": 9091}],
                        "volumeMounts": [
                            {"name": "config", "mountPath": "/usr/local/apisix/conf/config.yaml", "subPath": "config.yaml", "readOnly": True},
                            {"name": "config", "mountPath": "/usr/local/apisix/conf/apisix.yaml", "subPath": "apisix.yaml", "readOnly": True},
                        ],
                        "readinessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 5},
                        "resources": {"requests": {"cpu": "250m", "memory": "256Mi"}, "limits": {"cpu": "2", "memory": "1Gi"}},
                    }], "volumes": [{"name": "config", "configMap": {"name": gateway_name}}]},
                },
            },
        },
        {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": gateway_name, "namespace": namespace, "labels": gateway_labels},
            "spec": service_spec,
        },
    ]


def dev_jwks_resources(cluster: dict, versions: dict) -> list[dict]:
    www = REPO / "dev" / "auth" / "www"
    discovery = www / ".well-known" / "openid-configuration"
    jwks = www / "jwks.json"
    if not discovery.exists() or not jwks.exists():
        raise ValueError("DEV_AUTH public documents are missing; run python scripts/dev_auth.py init")
    namespace = cluster["namespace"]
    name = "dev-jwks"
    return [
        {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": namespace},
            "data": {
                "openid-configuration": discovery.read_text(),
                "jwks.json": jwks.read_text(),
                "serve.py": (REPO / "dev" / "jwks" / "serve.py").read_text(),
            },
        },
        {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "replicas": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
                "template": {
                    "metadata": {"labels": {"app.kubernetes.io/name": name}},
                    "spec": {"containers": [{
                        "name": name, "image": image_ref(versions, "python"), "command": ["python", "/app/serve.py"],
                        "ports": [{"name": "http", "containerPort": 8080}],
                        "volumeMounts": [
                            {"name": "config", "mountPath": "/app/serve.py", "subPath": "serve.py", "readOnly": True},
                            {"name": "config", "mountPath": "/www/.well-known/openid-configuration", "subPath": "openid-configuration", "readOnly": True},
                            {"name": "config", "mountPath": "/www/jwks.json", "subPath": "jwks.json", "readOnly": True},
                        ],
                    }], "volumes": [{"name": "config", "configMap": {"name": name}}]},
                },
            },
        },
        {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"selector": {"app.kubernetes.io/name": name}, "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]},
        },
    ]


def render_kubernetes(
    models_path: Path, teams_path: Path, versions_path: Path, profile_path: Path,
    auth_path: Path, auth_mode: str, output: Path,
) -> dict:
    models_doc = fleetplan.load_models(models_path)
    teams_doc = fleetplan.load_yaml(teams_path)
    versions = fleetplan.load_yaml(versions_path)
    profile = fleetplan.load_yaml(profile_path)
    auth = fleetplan.load_yaml(auth_path)
    _validate_profile(profile)
    cluster = profile["cluster"]
    plan, services = resolve_plan(models_doc, profile)
    if plan["models"] and cluster["model_storage"]["capacity_gb"] is None:
        raise ValueError(
            f"{cluster['name']}: model_storage.capacity_gb must record the purchased "
            "NVMe SKU before rendering active models"
        )
    digest = fleetplan.plan_hash(plan)

    resources: list[dict] = [{
        "apiVersion": "v1", "kind": "Namespace",
        "metadata": {"name": cluster["namespace"], "labels": {"app.kubernetes.io/part-of": "spark-fleet"}},
    }]
    if any(item["simulated"] for item in cluster["nodes"].values()):
        resources.append({
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "spark-fleet-mock", "namespace": cluster["namespace"]},
            "data": {"server.py": (REPO / "dev" / "mock_openai" / "server.py").read_text()},
        })
    resources.extend(model_staging_resources(cluster, versions, plan))
    workload_count = 0
    ray_count = 0
    for node_name, planned_node in sorted(plan["nodes"].items()):
        profile_node = cluster["nodes"][node_name]
        for container in planned_node["containers"]:
            service = services[(node_name, container["port"])]
            if container["tensor_parallel"] > 1 and cluster["pair_backend"] == "kuberay":
                resources.append(ray_service_resource(cluster, versions, node_name, profile_node, container, service.removesuffix("-serve-svc")))
                ray_count += 1
            else:
                resources.extend(standalone_resources(cluster, versions, node_name, profile_node, container, service))
            workload_count += 1

    config = generate_apisix.build_config_from_plan(plan, teams_doc, auth)
    rules = generate_apisix.render(config, digest, auth_mode)
    resources.extend(gateway_resources(cluster, versions, rules, auth_mode))
    if auth_mode == "dev":
        resources.extend(dev_jwks_resources(cluster, versions))

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    rendered = "\n---\n".join(yaml.safe_dump(item, sort_keys=False).rstrip() for item in resources) + "\n"
    (output / "fleet.yaml").write_text(rendered, encoding="utf-8", newline="\n")
    manifest = {
        "cluster": cluster["name"], "namespace": cluster["namespace"],
        "plan_hash": digest, "auth_mode": auth_mode,
        "workloads": workload_count, "ray_services": ray_count,
        "gpu_stack": cluster["gpu_stack"], "gpu_sharing": cluster["gpu_sharing"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=REPO / "clusters" / "dgx-spark" / "cluster.yaml")
    parser.add_argument("--models", type=Path, default=fleetplan.MODEL_CATALOG)
    parser.add_argument("--teams", type=Path, default=REPO / "teams.yaml")
    parser.add_argument("--versions", type=Path, default=REPO / "versions.yml")
    parser.add_argument("--auth-mode", choices=("dev", "prod"), default="dev")
    parser.add_argument("--auth", type=Path)
    parser.add_argument("--output", type=Path, default=REPO / "build" / "kubernetes")
    args = parser.parse_args()
    auth = args.auth or REPO / "gateway" / f"auth.{args.auth_mode}.yml"
    manifest = render_kubernetes(args.models, args.teams, args.versions, args.profile, auth, args.auth_mode, args.output)
    print(yaml.safe_dump(manifest, sort_keys=False).rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
