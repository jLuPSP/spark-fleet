#!/usr/bin/env python3
"""Sign in as a human user and obtain a delegated Spark Fleet access token.

MSAL tries the account/token cache first. When interaction is required it uses
device-code login by default. Pass --browser to opt into the Windows/WSL broker
or browser-based authorization code + PKCE. There is intentionally no client
secret in this flow.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import msal
from msal_extensions import PersistedTokenCache, build_encrypted_persistence

import harness_check

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = REPO / "private" / "entra.user.env"
DEFAULT_CACHE_FILE = REPO / "private" / "entra-user-token-cache.bin"


class UserAuthError(RuntimeError):
    """A concise user-facing delegated-authentication error."""


def is_wsl() -> bool:
    return bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in os.uname().release.lower()


def encrypted_cache(path: Path) -> PersistedTokenCache | None:
    """Use OS-backed encryption; never silently fall back to plaintext tokens."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return PersistedTokenCache(build_encrypted_persistence(str(path)))
    except Exception as error:  # platform keyring availability varies
        print(
            f"Warning: encrypted token cache unavailable ({error}); "
            "this sign-in will not persist.",
            file=sys.stderr,
        )
        return None


def public_client(client_id: str, tenant_id: str, cache_path: Path, device_code: bool):
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    if not device_code and (sys.platform == "win32" or is_wsl()):
        broker_option = (
            {"enable_broker_on_windows": True}
            if sys.platform == "win32"
            else {"enable_broker_on_wsl": True}
        )
        try:
            return msal.PublicClientApplication(
                client_id,
                authority=authority,
                **broker_option,
            ), True
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            print(f"Warning: OS authentication broker unavailable ({error}).", file=sys.stderr)

    return msal.PublicClientApplication(
        client_id,
        authority=authority,
        token_cache=encrypted_cache(cache_path),
    ), False


def acquire_user_token(
    values: dict[str, str],
    *,
    cache_path: Path,
    device_code: bool,
    force_login: bool,
) -> tuple[str, str, str]:
    tenant_id = harness_check.required(values, "ENTRA_TENANT_ID")
    api_client_id = harness_check.required(values, "ENTRA_API_CLIENT_ID")
    user_client_id = harness_check.required(values, "ENTRA_USER_CLIENT_ID")
    scope_name = os.environ.get("ENTRA_USER_SCOPE") or values.get(
        "ENTRA_USER_SCOPE", "access_as_user"
    )
    scope = f"api://{api_client_id}/{scope_name}"
    app, broker_enabled = public_client(user_client_id, tenant_id, cache_path, device_code)

    result = None
    if not force_login:
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent([scope], account=accounts[0])

    if not result:
        if device_code:
            flow = app.initiate_device_flow(scopes=[scope])
            if "user_code" not in flow:
                raise UserAuthError(
                    f"Entra could not start device login: {flow.get('error_description', flow)}"
                )
            print(flow["message"], file=sys.stderr, flush=True)
            result = app.acquire_token_by_device_flow(flow)
        else:
            options = {"scopes": [scope]}
            if broker_enabled:
                options["parent_window_handle"] = app.CONSOLE_WINDOW_HANDLE
            else:
                options["redirect_uri"] = "http://localhost"
            result = app.acquire_token_interactive(**options)

    token = result.get("access_token") if result else None
    if not token:
        message = (result or {}).get("error_description") or (result or {}).get("error")
        raise UserAuthError(f"Entra user sign-in failed: {message or 'no access token returned'}")
    return token, api_client_id, scope_name


def validate_user_claims(claims: dict, api_client_id: str, scope_name: str) -> None:
    harness_check.validate_claims(claims, api_client_id)
    if claims.get("idtyp") == "app" or not claims.get("oid"):
        raise UserAuthError("Entra returned an application token instead of a user token")
    scopes = set(str(claims.get("scp", "")).split())
    if scope_name not in scopes:
        raise UserAuthError(f"user token is missing delegated scope {scope_name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--cache-file", type=Path, default=DEFAULT_CACHE_FILE)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENAI_BASE_URL"),
    )
    parser.add_argument("--chat-model", default=os.environ.get("FLEET_CHAT_MODEL"))
    interaction = parser.add_mutually_exclusive_group()
    interaction.add_argument(
        "--browser",
        action="store_true",
        help="use the OS broker/browser instead of the default device-code login",
    )
    interaction.add_argument(
        "--device-code",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--force-login",
        action="store_true",
        help="ignore a cached account and show the account selector",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="print only the delegated access token for a BYO-key client",
    )
    args = parser.parse_args()

    try:
        values = harness_check.load_env_file(args.env_file)
        token, api_client_id, scope_name = acquire_user_token(
            values,
            cache_path=args.cache_file,
            device_code=not args.browser,
            force_login=args.force_login,
        )
        claims = harness_check.decode_claims(token)
        validate_user_claims(claims, api_client_id, scope_name)
        if args.print_token:
            print(token)
            return 0

        base_url = args.base_url or values.get("OPENAI_BASE_URL")
        chat_model = args.chat_model or values.get("FLEET_CHAT_MODEL")
        if not base_url or not chat_model:
            raise UserAuthError(
                "endpoint checks require OPENAI_BASE_URL and FLEET_CHAT_MODEL"
            )

        catalog = harness_check.api_json(base_url, token, "models")
        model_ids = sorted(item["id"] for item in catalog.get("data", []))
        identity = claims.get("preferred_username") or claims.get("name") or claims["oid"]
        print(f"Entra user: OK ({identity})")
        print(f"Delegated scope: {scope_name}")
        print(f"Roles: {', '.join(sorted(claims['roles']))}")
        print(f"GET /models: OK ({', '.join(model_ids)})")
        chat = harness_check.api_json(
            base_url,
            token,
            "chat/completions",
            {
                "model": chat_model,
                "messages": [
                    {"role": "user", "content": "Reply with exactly: delegated user ok"}
                ],
                "max_tokens": 16,
                "temperature": 0,
            },
        )
        if not chat.get("choices"):
            raise UserAuthError("chat response contained no choices")
        print("POST /chat/completions: OK (delegated user token)")
        return 0
    except (UserAuthError, harness_check.HarnessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
