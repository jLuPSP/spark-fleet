from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import harness_check  # noqa: E402


def jwt_with_claims(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_load_env_file_handles_export_quotes_and_secret_equals(tmp_path):
    path = tmp_path / "entra.env"
    path.write_text(
        "# ignored\nexport ENTRA_TENANT_ID='tenant'\n"
        'ENTRA_CALLER_CLIENT_SECRET="abc=def"\n',
        encoding="utf-8",
    )
    assert harness_check.load_env_file(path) == {
        "ENTRA_TENANT_ID": "tenant",
        "ENTRA_CALLER_CLIENT_SECRET": "abc=def",
    }


def test_decode_and_validate_claims():
    claims = {"aud": "api-client", "roles": ["Fleet.TeamAlpha"]}
    decoded = harness_check.decode_claims(jwt_with_claims(claims))
    harness_check.validate_claims(decoded, "api-client")


def test_validate_claims_rejects_wrong_audience_and_missing_role():
    with pytest.raises(harness_check.HarnessError, match="audience"):
        harness_check.validate_claims(
            {"aud": "something-else", "roles": ["Fleet.TeamAlpha"]}, "api-client"
        )
    with pytest.raises(harness_check.HarnessError, match="app role"):
        harness_check.validate_claims({"aud": "api-client", "roles": []}, "api-client")
