#!/usr/bin/env python3
"""Use the automation identity to verify the OpenAI-compatible endpoint.

This is deliberately the non-user, client-credentials check used by CI and
operators. Human coding clients should use entra_user_token.py instead.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = REPO / "private" / "entra.automation.env"


class HarnessError(RuntimeError):
    """A concise user-facing authentication or endpoint error."""


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise HarnessError(f"Entra environment file not found: {path}")
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            raise HarnessError(f"{path}:{number}: expected NAME=VALUE")
        name, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def required(values: dict[str, str], name: str) -> str:
    value = os.environ.get(name) or values.get(name)
    if not value:
        raise HarnessError(f"missing {name} in the environment or Entra env file")
    return value


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 60,
) -> dict:
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise HarnessError(f"{method} {url} returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise HarnessError(f"cannot reach {url}: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise HarnessError(f"{method} {url} did not return JSON") from error


def acquire_token(values: dict[str, str]) -> tuple[str, str]:
    tenant = required(values, "ENTRA_TENANT_ID")
    api_client = required(values, "ENTRA_API_CLIENT_ID")
    payload = urllib.parse.urlencode(
        {
            "client_id": required(values, "ENTRA_CALLER_CLIENT_ID"),
            "client_secret": required(values, "ENTRA_CALLER_CLIENT_SECRET"),
            "grant_type": "client_credentials",
            "scope": f"api://{api_client}/.default",
        }
    ).encode()
    response = http_json(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=payload,
    )
    token = response.get("access_token")
    if not token:
        raise HarnessError("Entra token response did not include access_token")
    return token, api_client


def decode_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, json.JSONDecodeError) as error:
        raise HarnessError("Entra returned a token with an unreadable JWT payload") from error


def validate_claims(claims: dict, api_client: str) -> None:
    audience = claims.get("aud")
    expected = {api_client, f"api://{api_client}"}
    actual = set(audience) if isinstance(audience, list) else {audience}
    if not actual & expected:
        raise HarnessError(f"token audience {audience!r} does not match the fleet API")
    roles = claims.get("roles")
    if not isinstance(roles, list) or not any(role.startswith("Fleet.") for role in roles):
        raise HarnessError("token has no Fleet.* app role")


def api_json(
    base_url: str,
    token: str,
    path: str,
    payload: dict | None = None,
    timeout: float = 120,
) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    body = None
    method = "GET"
    if payload is not None:
        method = "POST"
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    return http_json(
        f"{base_url.rstrip('/')}/{path.lstrip('/')}",
        method=method,
        headers=headers,
        body=body,
        timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENAI_BASE_URL"),
    )
    parser.add_argument("--chat-model", default=os.environ.get("FLEET_CHAT_MODEL"))
    parser.add_argument(
        "--embedding-model", default=os.environ.get("FLEET_EMBEDDING_MODEL")
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="authenticate and list models without sending inference requests",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="print only a fresh access token for a BYO-key harness",
    )
    args = parser.parse_args()

    try:
        values = load_env_file(args.env_file)
        token, api_client = acquire_token(values)
        claims = decode_claims(token)
        validate_claims(claims, api_client)
        if args.print_token:
            print(token)
            return 0

        base_url = args.base_url or values.get("OPENAI_BASE_URL")
        if not base_url:
            raise HarnessError("missing OPENAI_BASE_URL or --base-url")

        expires = datetime.fromtimestamp(int(claims.get("exp", 0)), timezone.utc)
        roles = ", ".join(sorted(claims["roles"]))
        print(f"Entra: OK (roles={roles}, expires={expires.isoformat()})")

        catalog = api_json(base_url, token, "models")
        model_ids = sorted(item["id"] for item in catalog.get("data", []))
        print(f"GET /models: OK ({', '.join(model_ids)})")
        if args.list_only:
            return 0

        chat_model = args.chat_model or values.get("FLEET_CHAT_MODEL")
        embedding_model = args.embedding_model or values.get("FLEET_EMBEDDING_MODEL")
        if not chat_model or not embedding_model:
            raise HarnessError(
                "inference checks require FLEET_CHAT_MODEL and FLEET_EMBEDDING_MODEL"
            )

        chat = api_json(
            base_url,
            token,
            "chat/completions",
            {
                "model": chat_model,
                "messages": [{"role": "user", "content": "Reply with exactly: fleet ok"}],
                "max_tokens": 16,
                "temperature": 0,
            },
        )
        choices = chat.get("choices") or []
        if not choices:
            raise HarnessError("chat response contained no choices")
        print(f"POST /chat/completions: OK (model={chat.get('model', chat_model)})")

        completion = api_json(
            base_url,
            token,
            "completions",
            {
                "model": chat_model,
                "prompt": "Reply with exactly: fleet ok",
                "max_tokens": 16,
                "temperature": 0,
            },
        )
        if not completion.get("choices"):
            raise HarnessError("completion response contained no choices")
        print(f"POST /completions: OK (model={completion.get('model', chat_model)})")

        embedding = api_json(
            base_url,
            token,
            "embeddings",
            {"model": embedding_model, "input": "spark fleet endpoint check"},
        )
        vectors = embedding.get("data") or []
        if not vectors or not vectors[0].get("embedding"):
            raise HarnessError("embedding response contained no vector")
        dimensions = len(vectors[0]["embedding"])
        print(f"POST /embeddings: OK (dimensions={dimensions})")
        print(f"Harness base URL: {base_url.rstrip('/')}")
        return 0
    except HarnessError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
