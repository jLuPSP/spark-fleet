# Authentication and authorization

APISIX validates Microsoft Entra access tokens before applying model policy. It
pins the tenant issuer and fleet API audience, then maps the token's `Fleet.*`
application role to the corresponding entry in `teams.yaml`.

## Registration model

Use three separate Entra registrations:

| Registration | Type | Purpose |
| --- | --- | --- |
| Fleet API | Protected API | Audience, delegated `access_as_user` scope and `Fleet.*` roles |
| User client | Public desktop client | Device-code or authorization-code login; no secret |
| Automation client | Confidential client | CI and unattended health checks using client credentials |

The API service principal should require assignment. Define each fleet role for
both `User` and `Application` when people and automation share the same APISIX
policy. Assign users or groups through the enterprise application, and grant the
automation service principal only the role it needs.

The public client receives delegated permission to
`api://<API_CLIENT_ID>/access_as_user`. Enable public-client flows/device code and
pre-authorize the client when the tenant's consent policy permits it. Never add a
client secret to a desktop/public registration.

## Human login

Copy `gateway/entra.user.env.example` to the ignored
`private/entra.user.env` and fill in its public identifiers and endpoint values.

```bash
python3 scripts/entra_user_token.py
```

MSAL checks its encrypted cache first and uses device code when interaction is
required. `--browser` opts into the OS broker/browser flow. The returned access
token must contain:

- the fleet API client ID as `aud`;
- delegated scope `access_as_user` in `scp`;
- the signed-in user's object ID in `oid`;
- an assigned `Fleet.*` value in `roles`.

An ID token proves login to the client but cannot authorize the API. A Microsoft
Graph token has the wrong audience and is also rejected.

To place only the access token in a BYO-key process environment:

```powershell
$env:SPARK_FLEET_TOKEN = .\scripts\entra-user-token.ps1 --print-token
```

## Automation

Copy `gateway/entra.automation.env.example` to the ignored
`private/entra.automation.env` or provide the same values through a secret manager.

```bash
python3 scripts/harness_check.py
```

This exchanges the automation client ID and secret for an app-only token and
tests the API. Client credentials contain no user context and must not be used by
Kilo, desktop tools, or people. Rotate this secret independently.

## Gateway enforcement

APISIX validates signature, issuer, audience and expiry. Generated Lua policy then:

1. resolves one known fleet role to a team;
2. overwrites untrusted inbound team/quota headers;
3. checks the team's model allowlist;
4. applies per-model request and token budgets;
5. forwards only to cataloged healthy services.

Missing or invalid authentication returns 401. A valid token without a recognized
role/model entitlement returns 403. Unknown model IDs receive an authenticated 404.

## Isolated DEV_AUTH

`scripts/dev_auth.py` generates a local signing key, discovery document and JWKS
under ignored `dev/auth/`. The Kubernetes renderer can deploy the tiny issuer with
`--auth-mode dev`; tokens are minted directly with:

```bash
python3 scripts/dev_auth.py mint --roles Fleet.TeamAlpha
```

DEV_AUTH exists only to test the JWT/policy path without a tenant. It is HTTP-only
and must never be exposed or promoted as production identity.

Primary references:

- [Authorization code with PKCE](https://learn.microsoft.com/entra/identity-platform/v2-oauth2-auth-code-flow)
- [MSAL Python token acquisition](https://learn.microsoft.com/entra/msal/python/getting-started/acquiring-tokens)
- [Entra application roles](https://learn.microsoft.com/entra/identity-platform/howto-add-app-roles-in-apps)
- [APISIX openid-connect plugin](https://apisix.apache.org/docs/apisix/plugins/openid-connect/)
