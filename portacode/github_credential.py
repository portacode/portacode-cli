"""Git credential helper backed by device-scoped Portacode GitHub grants."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .keypair import get_or_create_keypair, keypair_files_exist


DEFAULT_BASE_URL = "https://portacode.com"
BROKER_PATH = "/dashboard/github/device-credential/"


def _read_credential_input() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in sys.stdin:
        line = line.rstrip("\r\n")
        if not line:
            break
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def _repository_from_input(values: dict[str, str]) -> str | None:
    host = values.get("host", "").split(":", 1)[0].lower()
    if host != "github.com":
        return None
    path = values.get("path", "").strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if path.count("/") != 1:
        return None
    return path


def _signed_headers(body: bytes, *, path: str) -> dict[str, str]:
    if not keypair_files_exist():
        raise RuntimeError("This device is not paired with Portacode")
    keypair = get_or_create_keypair()
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"POST\n{path}\n{timestamp}\n{nonce}\n{body_hash}".encode("ascii")
    signature = base64.b64encode(keypair.sign_bytes(canonical)).decode("ascii")
    return {
        "X-Portacode-Device-Key": keypair.public_key_der_b64(),
        "X-Portacode-Timestamp": timestamp,
        "X-Portacode-Nonce": nonce,
        "X-Portacode-Signature": signature,
        "Content-Type": "application/json",
    }


def get_credential(repository: str) -> dict[str, str]:
    body = json.dumps({"repository": repository}, separators=(",", ":")).encode("utf-8")
    base_url = (os.environ.get("PORTACODE_GITHUB_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    response = httpx.post(
        f"{base_url}{BROKER_PATH}",
        content=body,
        headers=_signed_headers(body, path=BROKER_PATH),
        timeout=20.0,
    )
    payload = response.json()
    if response.status_code != 200 or not payload.get("ok"):
        raise RuntimeError(str(payload.get("error") or "GitHub access denied"))
    return payload


def _helper_command() -> str:
    """Return a PATH-independent helper for installed and source checkouts."""
    script_name = "git-credential-portacode.exe" if os.name == "nt" else "git-credential-portacode"
    beside_python = Path(sys.executable).resolve().parent / script_name
    if beside_python.is_file():
        return str(beside_python)
    discovered = shutil.which("git-credential-portacode")
    if discovered:
        return str(Path(discovered).resolve())
    source_root = Path(__file__).resolve().parents[1]
    if (source_root / "portacode" / "__init__.py").is_file() and os.name != "nt":
        return (
            f"!env PYTHONPATH={shlex.quote(str(source_root))} "
            f"{shlex.quote(str(Path(sys.executable).resolve()))} -m portacode.github_credential"
        )
    raise FileNotFoundError("git-credential-portacode is unavailable")


def configure_git() -> None:
    """Use this helper only for github.com and include owner/repository in requests."""
    from .connection.handlers.runtime_user import get_default_runtime_user, wrap_argv_for_user

    runtime_user = get_default_runtime_user()
    helper = _helper_command()
    subprocess.run(
        wrap_argv_for_user(
            ["git", "config", "--global", "credential.https://github.com.helper", helper],
            runtime_user,
        ),
        check=True,
    )
    subprocess.run(
        wrap_argv_for_user(
            ["git", "config", "--global", "credential.https://github.com.useHttpPath", "true"],
            runtime_user,
        ),
        check=True,
    )


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "get"
    values = _read_credential_input()
    if action != "get":
        return 0
    repository = _repository_from_input(values)
    if repository is None:
        return 0
    try:
        credential = get_credential(repository)
    except Exception as exc:
        print(f"git-credential-portacode: {exc}", file=sys.stderr)
        return 1
    print(f"username={credential['username']}")
    print(f"password={credential['password']}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
