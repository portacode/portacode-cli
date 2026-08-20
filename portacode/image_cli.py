"""CLI for Portacode's local, device-authenticated, usage-metered Images API."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Sequence

import requests


DEFAULT_BASE_URL = "http://127.0.0.1:61789/v1"


def _write_result(response: requests.Response, output: Path, *, force: bool) -> int:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("The Portacode image service returned invalid JSON.") from exc
    if response.status_code >= 400:
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else response.text
        raise RuntimeError(str(message or f"Image request failed ({response.status_code})."))
    data = payload.get("data") if isinstance(payload, dict) else None
    encoded = data[0].get("b64_json") if data and isinstance(data[0], dict) else None
    if not encoded:
        raise RuntimeError("The Portacode image service returned no image data.")
    if output.exists() and not force:
        raise FileExistsError(f"Refusing to overwrite existing file: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(base64.b64decode(encoded, validate=True))
    print(json.dumps({
        "path": str(output.resolve()),
        "model": "gpt-image-2",
        "usage": payload.get("usage") or {},
        "size": payload.get("size"),
        "quality": payload.get("quality"),
    }))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate or edit images through Portacode's metered local API."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation in ("generate", "edit"):
        command = subparsers.add_parser(operation)
        command.add_argument("prompt")
        command.add_argument("--out", required=True, type=Path)
        command.add_argument("--size", default="1024x1024")
        command.add_argument(
            "--quality", default="auto", choices=("auto", "low", "medium", "high")
        )
        command.add_argument("--force", action="store_true")
        if operation == "edit":
            command.add_argument("--image", action="append", required=True, type=Path)
    args = parser.parse_args(argv)
    base_url = args.base_url.rstrip("/")
    fields = {
        "model": "gpt-image-2", "prompt": args.prompt, "size": args.size,
        "quality": args.quality, "output_format": "png",
    }
    if args.operation == "generate":
        response = requests.post(
            f"{base_url}/images/generations", json=fields, timeout=None
        )
    else:
        opened = []
        try:
            files = []
            for image_path in args.image:
                handle = image_path.open("rb")
                opened.append(handle)
                files.append(("image[]", (image_path.name, handle, "application/octet-stream")))
            response = requests.post(
                f"{base_url}/images/edits", data=fields, files=files, timeout=None
            )
        finally:
            for handle in opened:
                handle.close()
    return _write_result(response, args.out, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
