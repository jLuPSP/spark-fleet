"""The public artifact must not contain workstation or home-lab values."""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKIP_PARTS = {".git", ".terraform", ".venv", "build", "private", "__pycache__"}
SKIP_SUFFIXES = {".pyc", ".tfstate", ".tfplan"}
TEXT_SUFFIXES = {
    "", ".example", ".json", ".jsonc", ".md", ".ps1", ".py", ".sh",
    ".tf", ".tmpl", ".tftpl", ".txt", ".yaml", ".yml",
}
FORBIDDEN = (
    "192." + "168.",
    "/mnt/user/" + "appdata",
    "/mnt/c/" + "users/",
    "root" + "@",
    "BEGIN " + "PRIVATE KEY",
)


def public_text_files():
    for root, directories, filenames in os.walk(REPO):
        directories[:] = [name for name in directories if name not in SKIP_PARTS]
        for filename in filenames:
            path = Path(root, filename)
            if path.name == "prompt.md" or ".private." in path.name or ".auto.tfvars" in path.name:
                continue
            if path.suffix in SKIP_SUFFIXES or path.suffix not in TEXT_SUFFIXES:
                continue
            yield path


def test_public_files_contain_no_local_or_private_values():
    findings = []
    for path in public_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for needle in FORBIDDEN:
            if needle.lower() in text:
                findings.append(f"{path.relative_to(REPO)} contains {needle!r}")
    assert not findings, "\n".join(findings)


def test_public_files_contain_no_email_addresses():
    import re

    email = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
    findings = []
    for path in public_text_files():
        if match := email.search(path.read_text(encoding="utf-8", errors="ignore")):
            findings.append(f"{path.relative_to(REPO)} contains {match.group(0)!r}")
    assert not findings, "\n".join(findings)
