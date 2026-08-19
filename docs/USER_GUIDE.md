# User guide

## What users receive

Users receive three things from the platform team:

- one HTTPS base URL ending in `/v1`;
- assignment to an Entra fleet role such as `Fleet.TeamAlpha`;
- model IDs visible to that role through `GET /models`.

They do not receive Kubernetes access, node addresses, model-server credentials,
or a shared client secret. The Entra access token is used as the `api_key` value
because OpenAI-compatible clients send it as `Authorization: Bearer`.

## Sign in

Copy `gateway/entra.user.env.example` to the ignored
`private/entra.user.env`, then fill in the public tenant, API, client and endpoint
values provided by the platform team. No secret belongs in this file.

From PowerShell:

```powershell
$env:SPARK_FLEET_TOKEN = .\scripts\entra-user-token.ps1 --print-token
```

The first run displays a Microsoft device-login URL and code. Later runs use the
OS-encrypted MSAL cache and silently refresh when possible. Pass `--browser` only
when an OS broker/browser flow is preferred.

## Kilo Code

Set the endpoint, acquire a token, and start Kilo from the same shell:

```powershell
$env:SPARK_FLEET_BASE_URL = "https://gateway.example/v1"
$env:SPARK_FLEET_TOKEN = .\scripts\entra-user-token.ps1 --print-token
kilo
```

Use `kilo.jsonc.example` as the project configuration. Its model declaration is
important: `tool_call`, context, and output limits must match the serving profile.
If the access token expires during a long session, reacquire it and restart Kilo.

Model capability declarations must match the serving profile. Context size,
maximum output, automatic tool choice, tool parser, and reasoning parser are
qualified together; a client must not advertise limits the backend cannot honor.

## OpenAI SDK

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["SPARK_FLEET_BASE_URL"],
    api_key=os.environ["SPARK_FLEET_TOKEN"],
)

print(client.models.list())
print(client.chat.completions.create(
    model="MODEL_ID",
    messages=[{"role": "user", "content": "hello"}],
    max_tokens=512,
))
```

The same client can call `/embeddings` with an embedding model returned by the
catalog. The complete request shapes are in `docs/openapi.yaml`.

## See request and token quota

APISIX returns the current quota state on model responses. Request-count headers
use a one-minute window, while AI token headers use the team/model token window
defined in `teams.yaml`:

```text
X-RateLimit-Limit: 120
X-RateLimit-Remaining: 117
X-RateLimit-Reset: 43
X-AI-Team-RateLimit-Limit: 2000000
X-AI-Team-RateLimit-Remaining: 1999692
X-AI-Team-RateLimit-Reset: 879
```

`Reset` is the remaining number of seconds in the current window. Quotas apply
to the resolved team and model route, not to a physical Spark. Kilo exposes these
headers in its API error details when a request fails, but may not display them
for a successful request; an HTTP client or SDK raw-response hook can always
inspect them.

The OpenAI API does not define a quota-inspection endpoint. A planned convenience
interface, `GET /fleet/v1/me` and a future `spark-fleet quota` command, will make
the same information visible without requiring users to inspect headers. Until
that extension exists, the response headers and `429` body are authoritative.

## Expected errors

| Status/error | Meaning | User action |
| --- | --- | --- |
| 401 | Missing, invalid or expired access token | Sign in again and restart the client |
| 403 | Token lacks an assigned fleet role or model access | Request the correct role/model entitlement |
| 404 unknown model | Model is absent from the role-filtered catalog | Use `GET /models`; do not guess IDs |
| 429 | Team request or token budget exceeded | Wait for reset or contact the model owner |
| Context overflow | Client prompt, tools, history and reserved output exceed the model window | Start a new task, compact earlier, or choose a larger model |
| Tool parser error | Client and serving capability declarations disagree | Report the model ID and response to the platform team |

## Automation is separate

CI and unattended health checks use `scripts/harness_check.py` with a confidential
automation identity stored in `private/entra.automation.env` or a secret manager. Its client
secret must never be pasted into Kilo or distributed to people.

See `docs/USER_SCENARIOS.md` for end-to-end examples covering interactive use,
automation, model rollout and training.
