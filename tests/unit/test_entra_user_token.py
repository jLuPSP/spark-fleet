from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import entra_user_token  # noqa: E402


def test_validate_user_claims_accepts_delegated_role_token():
    entra_user_token.validate_user_claims(
        {
            "aud": "api-client",
            "oid": "user-object-id",
            "scp": "access_as_user",
            "roles": ["Fleet.TeamAlpha"],
        },
        "api-client",
        "access_as_user",
    )


def test_validate_user_claims_rejects_app_token_and_missing_scope():
    with pytest.raises(entra_user_token.UserAuthError, match="application token"):
        entra_user_token.validate_user_claims(
            {
                "aud": "api-client",
                "idtyp": "app",
                "roles": ["Fleet.TeamAlpha"],
            },
            "api-client",
            "access_as_user",
        )
    with pytest.raises(entra_user_token.UserAuthError, match="delegated scope"):
        entra_user_token.validate_user_claims(
            {
                "aud": "api-client",
                "oid": "user-object-id",
                "roles": ["Fleet.TeamAlpha"],
            },
            "api-client",
            "access_as_user",
        )
