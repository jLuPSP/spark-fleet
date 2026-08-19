#!/bin/sh
# APISIX 3.17.0 workflow.log assumes workflow.access ran. An earlier OIDC/authz
# rejection legitimately skips access, leaving the cache nil and raising from the
# log phase. Guard that upstream edge so 401/403 responses remain observable.
set -eu

workflow=/usr/local/apisix/apisix/plugins/workflow.lua
if ! grep -q 'spark-fleet: access may be skipped' "$workflow"; then
  sed -i '/function _M.log(conf, ctx)/a\    -- spark-fleet: access may be skipped by an earlier auth plugin\
    if not ctx._workflow_cache then return end' "$workflow"
fi

# A request-count workflow rejection happens after ai-proxy-multi selects an
# instance but before an upstream response exists. The token limiter's log hook
# treats that valid 429 as missing usage and logs an error; no token accounting is
# possible or needed, so skip the hook when neither usage representation exists.
rate_limit=/usr/local/apisix/apisix/plugins/ai-rate-limiting.lua
if ! grep -q 'spark-fleet: request stopped before token usage' "$rate_limit"; then
  sed -i '/function _M.log(conf, ctx)/a\    -- spark-fleet: request stopped before token usage was available\
    if not ctx.ai_token_usage and not ctx.llm_raw_usage then return end' "$rate_limit"
fi

# APISIX 3.17 registers per-route Prometheus collectors on the first standalone
# YAML mtime reload after workers initialize. Trigger exactly one delayed reload.
(sleep 8; touch /usr/local/apisix/conf/apisix.yaml; sleep 2; touch /tmp/metrics-ready) &
exec /docker-entrypoint.sh docker-start
