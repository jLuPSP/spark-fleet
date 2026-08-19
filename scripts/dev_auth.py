"""DEV_AUTH tooling: a self-issued OIDC issuer for local development.

Subcommands:
  init   Generate an RSA keypair (dev/auth/keys/, gitignored) and write the static
         issuer content (dev/auth/www/): jwks.json and the OIDC discovery document.
         The dev-jwks container serves dev/auth/www; APISIX validates bearer tokens
         against it exactly the way it validates Entra tokens in prod.
  mint   Sign a JWT with the dev key. --team looks up the Entra app role in
         teams.yaml; --roles passes raw role values.

Nothing here is a secret: the key is generated locally, gitignored, and only ever
trusted by the local dev gateway.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
from pathlib import Path

import jwt
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPO = Path(__file__).resolve().parent.parent
KEYS = REPO / "dev" / "auth" / "keys"
WWW = REPO / "dev" / "auth" / "www"
KEY_FILE = KEYS / "dev-rsa.pem"

# The issuer URL as seen from inside the dev docker network (APISIX fetches the
# discovery document from here). Tokens must carry exactly this iss.
DEFAULT_ISSUER = "http://dev-jwks:8080"
DEFAULT_AUDIENCE = "spark-fleet-dev"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_to_b64url(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return _b64url(value.to_bytes(length, "big"))


def load_key() -> rsa.RSAPrivateKey:
    if not KEY_FILE.exists():
        raise SystemExit("dev key missing; run: python scripts/dev_auth.py init")
    return serialization.load_pem_private_key(KEY_FILE.read_bytes(), password=None)


def kid_for(key: rsa.RSAPrivateKey) -> str:
    numbers = key.public_key().public_numbers()
    material = f"{numbers.n}:{numbers.e}".encode()
    return hashlib.sha256(material).hexdigest()[:16]


def cmd_init(args) -> int:
    KEYS.mkdir(parents=True, exist_ok=True)
    (WWW / ".well-known").mkdir(parents=True, exist_ok=True)

    if KEY_FILE.exists() and not args.force:
        key = load_key()
        print(f"dev key exists ({KEY_FILE}); reusing (use --force to rotate)")
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        KEY_FILE.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        print(f"wrote {KEY_FILE}")

    numbers = key.public_key().public_numbers()
    kid = kid_for(key)
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": _int_to_b64url(numbers.n),
                "e": _int_to_b64url(numbers.e),
            }
        ]
    }
    (WWW / "jwks.json").write_text(json.dumps(jwks, indent=2) + "\n", encoding="utf-8")

    issuer = args.issuer
    discovery = {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/jwks.json",
        # Dummy endpoints: bearer-only validation never calls these, but OIDC
        # libraries expect the keys to exist in the document.
        "authorization_endpoint": f"{issuer}/dev/authorize",
        "token_endpoint": f"{issuer}/dev/token",
        "userinfo_endpoint": f"{issuer}/dev/userinfo",
        "response_types_supported": ["token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    (WWW / ".well-known" / "openid-configuration").write_text(
        json.dumps(discovery, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {WWW / 'jwks.json'}")
    print(f"wrote {WWW / '.well-known' / 'openid-configuration'} (issuer {issuer})")
    return 0


def cmd_mint(args) -> int:
    key = load_key()

    roles = []
    if args.team:
        teams = (yaml.safe_load((REPO / "teams.yaml").read_text(encoding="utf-8")) or {}).get(
            "teams", []
        )
        match = next((t for t in teams if t["name"] == args.team), None)
        if match is None:
            known = ", ".join(t["name"] for t in teams)
            raise SystemExit(f"unknown team {args.team!r} (teams.yaml has: {known})")
        roles.append(match["entra_app_role"])
    if args.roles:
        roles.extend(r for r in args.roles.split(",") if r)
    if not roles and not args.no_roles:
        raise SystemExit("pass --team, --roles, or --no-roles")

    now = int(time.time())
    claims = {
        "iss": args.issuer,
        "aud": args.audience,
        "sub": args.sub,
        "name": args.sub,
        "iat": now,
        "nbf": now,
        "exp": now + args.ttl,
    }
    if roles:
        claims["roles"] = roles

    token = jwt.encode(
        claims,
        key,
        algorithm="RS256",
        headers={"kid": kid_for(key), "typ": "JWT"},
    )
    print(token)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="generate dev key + JWKS + discovery doc")
    p_init.add_argument("--issuer", default=DEFAULT_ISSUER)
    p_init.add_argument("--force", action="store_true", help="rotate the key")
    p_init.set_defaults(fn=cmd_init)

    p_mint = sub.add_parser("mint", help="mint a dev bearer token")
    p_mint.add_argument("--team", help="team name from teams.yaml")
    p_mint.add_argument("--roles", help="comma-separated raw role values")
    p_mint.add_argument("--no-roles", action="store_true", help="mint a roleless token")
    p_mint.add_argument("--sub", default="dev-user")
    p_mint.add_argument("--ttl", type=int, default=3600)
    p_mint.add_argument("--issuer", default=DEFAULT_ISSUER)
    p_mint.add_argument("--audience", default=DEFAULT_AUDIENCE)
    p_mint.set_defaults(fn=cmd_mint)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
