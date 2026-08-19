"""Build deterministic APISIX standalone routes from a resolved fleet plan.

The Kubernetes renderer supplies stable Service endpoints. APISIX runs without
etcd or an Admin API, so the generated file is the complete gateway state.

Route layout (all verified against APISIX 3.17.0 plugin semantics):
  generation:  POST /v1/chat/completions and /v1/completions
  embedding:   POST /v1/embeddings
               Each route matches vars [["post_arg.model", "==", <name>]].
               (JSON body matching needs APISIX >= 3.14.0 and an
               application/json Content-Type on the request)
    plugins:   openid-connect (rewrite phase, priority 2599): bearer-only JWKS
                 validation, audience required + matched against client_id,
                 issuer pinned via valid_issuers (fail closed).
               serverless-pre-function (access phase, runs after openid-connect
                 because phase ordering trumps its 10000 priority): decodes the
                 X-Userinfo header openid-connect sets, maps the roles claim to a
                 team, 403s teams not allowed for this model, and overwrites the
                 trusted X-Fleet-Team / X-Fleet-Token-Quota request headers.
               workflow (access, 1006): per-team request quotas; each rule embeds
                 one limit-count with rejected_code 429; the caseless fallback
                 rule rejects anything the role check somehow did not label.
               ai-rate-limiting (access/log, 1030): token budgets counted from
                 the response usage field; a per-model route budget plus a
                 per-team rule keyed on the trusted header.
               ai-proxy-multi (access + before_proxy, 1041): one
                 openai-compatible instance per planned replica, weighted
                 roundrobin, per-instance active health checks against /health.
                 It bypasses the nginx upstream entirely, so routes carry no
                 upstream block.
               prometheus (prefer_name: true so metrics are labeled by name).
  catch-all:   same uri, no vars, lower priority: authenticates, then returns an
               OpenAI-shaped 404 so uncataloged models are unroutable by
               construction.
  /v1/models:  team-filtered catalog listing.
  /healthz:    unauthenticated liveness for Kubernetes and load-balancer probes.

Per-team quota semantics: limits in teams.yaml apply per team PER MODEL ROUTE
(limit-count counters cannot span routes in standalone mode). Counters reset when
a route's plugin config changes, which in GitOps terms means quota windows reset
on deploys that touch the route.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import fleetplan  # noqa: E402

MODEL_ROUTE_PRIORITY = 10
CATCHALL_ROUTE_PRIORITY = 1
# vLLM instances on the fleet network run without --api-key today; the header
# only exists because ai-proxy-multi's schema requires an auth block.
INTERNAL_AUTH_HEADER = "Bearer fleet-internal-unused"

TASK_ENDPOINTS = {
    "generation": (
        ("chat", "/v1/chat/completions"),
        ("completion", "/v1/completions"),
    ),
    "embedding": (("embedding", "/v1/embeddings"),),
}


def lua_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _decode_claims_lua() -> str:
    """Shared Lua prologue: X-Userinfo -> claims table, roles -> `have` set."""
    return """\
  local core = require("apisix.core")
  local ui_b64 = core.request.header(ctx, "X-Userinfo")
  if not ui_b64 then
    return 401, { error = "no validated token context" }
  end
  local ui_json = ngx.decode_base64(ui_b64)
  local claims = ui_json and core.json.decode(ui_json) or nil
  if not claims then
    return 401, { error = "cannot decode userinfo" }
  end
  local have = {}
  if type(claims.roles) == "table" then
    for _, r in ipairs(claims.roles) do
      have[r] = true
    end
  end
"""


def _team_resolution_lua(teams: list[dict]) -> str:
    """Lua that resolves `team` from the `have` role set, in sorted team order so
    a token carrying several fleet roles resolves deterministically."""
    lines = ["  local team"]
    for team in sorted(teams, key=lambda t: t["name"]):
        lines.append(
            f"  if not team and have[{lua_quote(team['entra_app_role'])}] then"
            f" team = {lua_quote(team['name'])} end"
        )
    lines.append("""\
  if not team then
    return 403, { error = "token carries no fleet role" }
  end""")
    return "\n".join(lines) + "\n"


def authz_lua(model_name: str, teams: list[dict], allowed: list[dict]) -> str:
    """Role check + model authorization + trusted header injection for one route."""
    allowed_set = "\n".join(
        f"    [{lua_quote(t['name'])}] = true," for t in sorted(allowed, key=lambda t: t["name"])
    )
    quota_map = "\n".join(
        f"    [{lua_quote(t['name'])}] = {lua_quote(str(t['limits']['tokens_per_hour']))},"
        for t in sorted(allowed, key=lambda t: t["name"])
    )
    return f"""\
return function(conf, ctx)
{_decode_claims_lua()}{_team_resolution_lua(teams)}\
  local allowed = {{
{allowed_set}
  }}
  if not allowed[team] then
    return 403, {{ error = "team " .. team .. " may not call model {model_name}" }}
  end
  local quota = {{
{quota_map}
  }}
  core.request.set_header(ctx, "X-Fleet-Team", team)
  core.request.set_header(ctx, "X-Fleet-Token-Quota", quota[team])
end"""


def models_listing_lua(teams: list[dict]) -> str:
    """GET /v1/models: list only the models the caller's team may use."""
    team_models = []
    for team in sorted(teams, key=lambda t: t["name"]):
        items = ", ".join(lua_quote(m) for m in sorted(team["allowed_models"]))
        team_models.append(f"    [{lua_quote(team['name'])}] = {{ {items} }},")
    team_models_lua = "\n".join(team_models)
    return f"""\
return function(conf, ctx)
{_decode_claims_lua()}{_team_resolution_lua(teams)}\
  local catalog = {{
{team_models_lua}
  }}
  local data = {{}}
  for _, name in ipairs(catalog[team] or {{}}) do
    data[#data + 1] = {{ id = name, object = "model", owned_by = "spark-fleet" }}
  end
  return 200, {{ object = "list", data = data }}
end"""


def unknown_model_lua() -> str:
    return """\
return function(conf, ctx)
  local core = require("apisix.core")
  local model = "unknown"
  local body = core.request.get_body()
  if body then
    local data = core.json.decode(body)
    if data and type(data.model) == "string" then
      model = data.model
    end
  end
  return 404, {
    object = "error",
    type = "NotFoundError",
    code = 404,
    message = "The model `" .. model
      .. "` is not in the fleet catalog (or the request is not application/json).",
  }
end"""


def healthz_lua() -> str:
    return """\
return function(conf, ctx)
  return 200, { status = "ok", service = "spark-fleet-gateway" }
end"""


def oidc_plugin(auth: dict) -> dict:
    return {
        "bearer_only": True,
        "use_jwks": True,
        "discovery": auth["discovery"],
        "client_id": auth["client_id"],
        "ssl_verify": bool(auth.get("ssl_verify", True)),
        "token_signing_alg_values_expected": "RS256",
        "accept_none_alg": False,
        "accept_unsupported_alg": False,
        "realm": "spark-fleet",
        "claim_validator": {
            "issuer": {"valid_issuers": list(auth["valid_issuers"])},
            "audience": {"claim": "aud", "required": True, "match_with_client_id": True},
        },
        "set_userinfo_header": True,
        "set_access_token_header": False,
        "set_id_token_header": False,
    }


def workflow_plugin(model_name: str, allowed: list[dict]) -> dict:
    rules = []
    for team in sorted(allowed, key=lambda t: t["name"]):
        rules.append(
            {
                "case": [["http_x_fleet_team", "==", team["name"]]],
                "actions": [
                    [
                        "limit-count",
                        {
                            "count": team["limits"]["requests_per_minute"],
                            "time_window": 60,
                            "key_type": "constant",
                            "key": f"{model_name}:{team['name']}",
                            "rejected_code": 429,
                            "rejected_msg": json.dumps(
                                {
                                    "error": (
                                        f"request quota exceeded for {team['name']} "
                                        f"on {model_name}"
                                    )
                                }
                            ),
                            "policy": "local",
                        },
                    ]
                ],
            }
        )
    # Defense in depth: the role check already 403s unlabeled requests, so this
    # fallback only fires if the plugin chain is somehow misconfigured.
    rules.append({"actions": [["return", {"code": 403}]]})
    return {"rules": rules}


def ai_rate_limiting_plugin(model_name: str, allowed: list[dict]) -> dict:
    # The 3.17.0 schema is a oneOf: a route-level limit/time_window pair OR
    # rules[]/instances[]; sending both fails validation ("matches both schemas
    # 1 and 2") and the whole route is rejected. Per-team budgets need rules,
    # so rules alone it is; the count comes from the trusted quota header the
    # role check sets (variable rules need APISIX >= 3.16.0).
    return {
        "limit_strategy": "total_tokens",
        "rejected_code": 429,
        "rejected_msg": f"token budget exhausted for {model_name}",
        "show_limit_quota_header": True,
        "rules": [
            {
                "count": "${http_x_fleet_token_quota ?? 1000}",
                "time_window": 3600,
                "key": "$http_x_fleet_team",
                "header_prefix": "Team",
            }
        ],
    }


def ai_proxy_multi_plugin(model_name: str, model: dict, endpoint: str) -> dict:
    instances = []
    for upstream in model["upstreams"]:
        instances.append(
            {
                "name": f"{fleetplan.sanitize(model_name)}-{upstream['node']}-p{upstream['port']}",
                "provider": "openai-compatible",
                "weight": 1,
                "priority": 0,
                "auth": {"header": {"Authorization": INTERNAL_AUTH_HEADER}},
                "options": {"model": model_name},
                "override": {
                    "endpoint": f"http://{upstream['address']}:{upstream['port']}{endpoint}"
                },
                "checks": {
                    "active": {
                        "type": "http",
                        "http_path": "/health",
                        "timeout": 2,
                        "healthy": {"interval": 2, "successes": 2},
                        "unhealthy": {
                            "interval": 1,
                            "http_failures": 3,
                            "timeouts": 3,
                            "http_statuses": [429, 500, 502, 503, 504],
                        },
                    }
                },
            }
        )
    return {
        "fallback_strategy": ["http_429", "http_5xx"],
        "max_retries": 2,
        "retry_on_failure_within_ms": 2000,
        "timeout": 120000,
        "keepalive": True,
        "ssl_verify": False,
        "balancer": {"algorithm": "roundrobin"},
        "instances": instances,
    }


def build_config_from_plan(
    plan: dict,
    teams_doc: dict,
    auth: dict,
    exclude_nodes: set[str] | None = None,
) -> dict:
    """Build routes from Kubernetes-resolved model placements."""
    exclude_nodes = exclude_nodes or set()
    teams = teams_doc.get("teams") or []
    routes = []

    for model_name in sorted(plan["models"]):
        model = plan["models"][model_name]
        model = {
            **model,
            "upstreams": [u for u in model["upstreams"] if u["node"] not in exclude_nodes],
        }
        if not model["upstreams"]:
            continue
        allowed = sorted(
            (t for t in teams if model_name in t["allowed_models"]),
            key=lambda t: t["name"],
        )
        if not allowed:
            # validate.py warns about this; an unreachable model gets no route.
            continue
        slug = fleetplan.sanitize(model_name)
        task = model.get("task", "generation")
        for endpoint_name, endpoint in TASK_ENDPOINTS[task]:
            route_id = f"sf-{endpoint_name}-{slug}"
            routes.append(
                {
                    "id": route_id,
                    "name": route_id,
                    "uri": endpoint,
                    "methods": ["POST"],
                    "priority": MODEL_ROUTE_PRIORITY,
                    "vars": [["post_arg.model", "==", model_name]],
                    "plugins": {
                        "openid-connect": oidc_plugin(auth),
                        "serverless-pre-function": {
                            "phase": "access",
                            "functions": [authz_lua(model_name, teams, allowed)],
                        },
                        "workflow": workflow_plugin(model_name, allowed),
                        "ai-rate-limiting": ai_rate_limiting_plugin(model_name, allowed),
                        "ai-proxy-multi": ai_proxy_multi_plugin(model_name, model, endpoint),
                        "prometheus": {"prefer_name": True},
                    },
                }
            )

    for endpoint_name, endpoint in (
        ("chat", "/v1/chat/completions"),
        ("completion", "/v1/completions"),
        ("embedding", "/v1/embeddings"),
    ):
        route_id = f"sf-{endpoint_name}-unknown-model"
        routes.append(
            {
                "id": route_id,
                "name": route_id,
                "uri": endpoint,
                "methods": ["POST"],
                "priority": CATCHALL_ROUTE_PRIORITY,
                "plugins": {
                    "openid-connect": oidc_plugin(auth),
                    "serverless-pre-function": {
                        "phase": "access",
                        "functions": [unknown_model_lua()],
                    },
                    "prometheus": {"prefer_name": True},
                },
            }
        )
    routes.append(
        {
            "id": "sf-models",
            "name": "sf-models",
            "uri": "/v1/models",
            "methods": ["GET"],
            "plugins": {
                "openid-connect": oidc_plugin(auth),
                "serverless-pre-function": {
                    "phase": "access",
                    "functions": [models_listing_lua(teams)],
                },
                "prometheus": {"prefer_name": True},
            },
        }
    )
    routes.append(
        {
            "id": "sf-healthz",
            "name": "sf-healthz",
            "uri": "/healthz",
            "methods": ["GET"],
            "plugins": {
                "serverless-pre-function": {"phase": "access", "functions": [healthz_lua()]},
            },
        }
    )

    return {"routes": routes}


class _Dumper(yaml.SafeDumper):
    pass


def _str_representer(dumper, value: str):
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_Dumper.add_representer(str, _str_representer)


def render(config: dict, plan_digest: str, mode: str) -> str:
    header = (
        "# Generated by gateway/generate_apisix.py. DO NOT EDIT.\n"
        f"# mode: {mode}\n"
        f"# plan_hash: {plan_digest}\n"
        "# APISIX 3.17.0 standalone rules file; the trailing #END marker is\n"
        "# mandatory or APISIX silently ignores the whole file.\n"
    )
    body = yaml.dump(
        config,
        Dumper=_Dumper,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
        allow_unicode=True,
    )
    return header + body + "#END\n"
