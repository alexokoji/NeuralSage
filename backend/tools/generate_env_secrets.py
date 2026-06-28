#!/usr/bin/env python3
"""Generate JWT and ENCRYPTION keys and safely write backend/.env.

Usage:
  python tools/generate_env_secrets.py [--force] [--no-write]

Options:
  --force    Write without prompting (useful in CI).
  --no-write Print the generated values and show the intended changes but do not write.
"""
from __future__ import annotations

import argparse
import base64
import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"
TARGET = ROOT / ".env"
BACKUP_FMT = ".env.bak.{ts}"

JWT_KEY_VAR = "JWT_SECRET_KEY"
ENC_KEY_VAR = "ENCRYPTION_KEY"


def generate_jwt_secret() -> str:
    return secrets.token_urlsafe(64)


def generate_encryption_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def read_env_template() -> str:
    if TARGET.exists():
        return TARGET.read_text()
    if EXAMPLE.exists():
        return EXAMPLE.read_text()
    return ""


def replace_or_add(lines: list[str], key: str, value: str) -> list[str]:
    prefix = key + "="
    found = False
    out = []
    for ln in lines:
        if ln.strip().startswith(prefix):
            out.append(f"{key}={value}\n")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}\n")
    return out


def backup_target(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    bak = path.with_name(BACKUP_FMT.format(ts=ts))
    shutil.copy2(path, bak)
    return bak


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Write without prompt")
    parser.add_argument("--no-write", action="store_true", help="Don't write, only print")
    args = parser.parse_args()

    jwt = generate_jwt_secret()
    enk = generate_encryption_key()

    content = read_env_template()
    lines = content.splitlines(keepends=True) if content else []

    new_lines = replace_or_add(lines, JWT_KEY_VAR, jwt)
    new_lines = replace_or_add(new_lines, ENC_KEY_VAR, enk)

    print("Generated values:")
    print(f"  {JWT_KEY_VAR}={jwt}")
    print(f"  {ENC_KEY_VAR}={enk}")
    print()

    if args.no_write:
        print("--no-write specified; not writing to disk.")
        return 0

    if not args.force:
        ans = input(f"Write these values into {TARGET}? [y/N]: ").strip().lower()
        if ans != "y":
            print("Aborted by user.")
            return 0

    bak = backup_target(TARGET)
    if bak:
        print(f"Backed up existing {TARGET} to {bak}")

    TARGET.write_text("".join(new_lines))
    print(f"Wrote updated env to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
