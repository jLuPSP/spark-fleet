"""Golden-file and structural tests for the APISIX config generator.

The golden files pin the exact bytes the generator emits for the checked-in
catalog. Any intentional change to the generator or the catalog updates them via:

    UPDATE_GOLDEN=1 python -m pytest tests/unit -k golden
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "gateway"))
sys.path.insert(0, str(Path(__file__).parent))

import fleetplan  # noqa: E402
import generate_apisix as gen  # noqa: E402
import render_kubernetes  # noqa: E402
from catalog_fixtures import active_catalog  # noqa: E402

GOLDEN = REPO / "tests" / "golden"

PROFILE = REPO / "clusters" / "dgx-spark"
CASES = {
    "apisix.dev.yaml": ("dev", "gateway/auth.dev.yml"),
    "apisix.prod.yaml": ("prod", "gateway/auth.prod.yml.example"),
}


def _documents():
    models = active_catalog(fleetplan)
    teams = fleetplan.load_yaml(REPO / "teams.yaml")
    profile = fleetplan.load_yaml(PROFILE / "cluster.yaml")
    plan, _ = render_kubernetes.resolve_plan(models, profile)
    return models, teams, plan


def _render(mode: str, auth: str) -> str:
    _, teams, plan = _documents()
    auth_doc = fleetplan.load_yaml(REPO / auth)
    config = gen.build_config_from_plan(plan, teams, auth_doc)
    return gen.render(config, fleetplan.plan_hash(plan), mode)


@pytest.mark.parametrize("golden_name", sorted(CASES))
def test_golden(golden_name: str):
    output = _render(*CASES[golden_name])
    path = GOLDEN / golden_name
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8", newline="\n")
    assert path.exists(), f"{path} missing; regenerate with UPDATE_GOLDEN=1"
    assert output == path.read_text(encoding="utf-8"), (
        f"{golden_name} drifted from the generator; if the change is intentional, "
        "regenerate with UPDATE_GOLDEN=1 and review the diff"
    )


def test_deterministic():
    first = _render(*CASES["apisix.dev.yaml"])
    second = _render(*CASES["apisix.dev.yaml"])
    assert first == second


def test_end_marker():
    output = _render(*CASES["apisix.dev.yaml"])
    assert output.endswith("#END\n"), "APISIX silently ignores the file without #END"


def test_structure():
    output = _render(*CASES["apisix.dev.yaml"])
    doc = yaml.safe_load(output)
    routes = {r["id"]: r for r in doc["routes"]}

    models, teams, plan = _documents()

    # Generation models get chat + completions routes. Every supported endpoint
    # also gets an authenticated unknown-model catch-all.
    generation_names = sorted(
        name for name, model in plan["models"].items() if model["task"] == "generation"
    )
    embedding_names = sorted(
        name for name, model in plan["models"].items() if model["task"] == "embedding"
    )
    assert len(routes) == (2 * len(generation_names)) + len(embedding_names) + 5

    for name in generation_names:
        route = routes[f"sf-chat-{fleetplan.sanitize(name)}"]
        assert route["vars"] == [["post_arg.model", "==", name]]
        assert route["priority"] > routes["sf-chat-unknown-model"]["priority"]
        plugins = route["plugins"]
        for required in (
            "openid-connect",
            "serverless-pre-function",
            "workflow",
            "ai-rate-limiting",
            "ai-proxy-multi",
            "prometheus",
        ):
            assert required in plugins, f"{name}: missing {required}"
        assert plugins["openid-connect"]["bearer_only"] is True
        assert plugins["openid-connect"]["claim_validator"]["audience"]["required"] is True

        # ai-proxy-multi bypasses nginx upstreams; the route must not carry one.
        assert "upstream" not in route and "upstream_id" not in route

        # Every planned replica appears exactly once as an instance endpoint.
        endpoints = {
            i["override"]["endpoint"] for i in plugins["ai-proxy-multi"]["instances"]
        }
        expected = {
            f"http://{u['address']}:{u['port']}/v1/chat/completions"
            for u in plan["models"][name]["upstreams"]
        }
        assert endpoints == expected

        completion = routes[f"sf-completion-{fleetplan.sanitize(name)}"]
        assert completion["uri"] == "/v1/completions"
        completion_endpoints = {
            i["override"]["endpoint"]
            for i in completion["plugins"]["ai-proxy-multi"]["instances"]
        }
        assert completion_endpoints == {
            f"http://{u['address']}:{u['port']}/v1/completions"
            for u in plan["models"][name]["upstreams"]
        }

        # workflow: one limit-count rule per allowed team plus the fallback deny.
        allowed = [t for t in teams["teams"] if name in t["allowed_models"]]
        rules = plugins["workflow"]["rules"]
        assert len(rules) == len(allowed) + 1
        assert "case" not in rules[-1], "fallback deny rule must be last and caseless"
        for rule in rules[:-1]:
            action_name, conf = rule["actions"][0]
            assert action_name == "limit-count"
            assert conf["rejected_code"] == 429

    for name in embedding_names:
        slug = fleetplan.sanitize(name)
        route = routes[f"sf-embedding-{slug}"]
        assert route["vars"] == [["post_arg.model", "==", name]]
        assert f"sf-chat-{slug}" not in routes
        assert f"sf-completion-{slug}" not in routes
        assert all(
            item["override"]["endpoint"].endswith("/v1/embeddings")
            for item in route["plugins"]["ai-proxy-multi"]["instances"]
        )

    assert routes["sf-models"]["uri"] == "/v1/models"
    healthz = routes["sf-healthz"]
    assert "openid-connect" not in healthz["plugins"], "/healthz must stay public"


def test_unroutable_model_gets_no_route():
    """A cataloged model no team may call must not produce a route."""
    models = active_catalog(fleetplan)
    teams = {"teams": []}
    profile = fleetplan.load_yaml(PROFILE / "cluster.yaml")
    plan, _ = render_kubernetes.resolve_plan(models, profile)
    auth = fleetplan.load_yaml(REPO / "gateway" / "auth.dev.yml")
    config = gen.build_config_from_plan(plan, teams, auth)
    ids = {r["id"] for r in config["routes"]}
    assert ids == {
        "sf-chat-unknown-model",
        "sf-completion-unknown-model",
        "sf-embedding-unknown-model",
        "sf-models",
        "sf-healthz",
    }


def test_embedding_model_gets_only_embedding_route():
    models = active_catalog(fleetplan)
    models["models"][0]["task"] = "embedding"
    name = models["models"][0]["name"]
    teams = fleetplan.load_yaml(REPO / "teams.yaml")
    profile = fleetplan.load_yaml(PROFILE / "cluster.yaml")
    plan, _ = render_kubernetes.resolve_plan(models, profile)
    auth = fleetplan.load_yaml(REPO / "gateway" / "auth.dev.yml")
    routes = {
        r["id"]: r for r in gen.build_config_from_plan(plan, teams, auth)["routes"]
    }
    slug = fleetplan.sanitize(name)
    assert f"sf-embedding-{slug}" in routes
    assert f"sf-chat-{slug}" not in routes
    assert f"sf-completion-{slug}" not in routes
    endpoints = {
        i["override"]["endpoint"]
        for i in routes[f"sf-embedding-{slug}"]["plugins"]["ai-proxy-multi"]["instances"]
    }
    assert endpoints
    assert all(endpoint.endswith("/v1/embeddings") for endpoint in endpoints)


def test_no_private_values_in_output():
    """The generated prod config must never leak anything the publish scan flags."""
    output = _render(*CASES["apisix.prod.yaml"])
    # Keep the publish scanner's forbidden literals out of this tracked test too.
    for needle in (
        "tower" + ".local",
        "192." + "168.",
        "/mnt/user/" + "appdata",
        "root" + "@",
    ):
        assert needle not in output


def test_drain_removes_only_the_selected_node():
    models = active_catalog(fleetplan)
    teams = fleetplan.load_yaml(REPO / "teams.yaml")
    profile = fleetplan.load_yaml(REPO / "clusters" / "dgx-spark" / "cluster.yaml")
    plan, _ = render_kubernetes.resolve_plan(models, profile)
    auth = fleetplan.load_yaml(REPO / "gateway" / "auth.dev.yml")
    config = gen.build_config_from_plan(plan, teams, auth, {"node1"})
    routes = {route["id"]: route for route in config["routes"]}
    instances = routes["sf-chat-qwen3-14b-awq"]["plugins"]["ai-proxy-multi"]["instances"]
    assert instances
    assert all("node1" not in item["name"] for item in instances)

    # Both generation deployments must be unavailable before the route is removed.
    drained = gen.build_config_from_plan(plan, teams, auth, {"node1", "node3"})
    assert "sf-chat-qwen3-14b-awq" not in {route["id"] for route in drained["routes"]}
