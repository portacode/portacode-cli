#!/usr/bin/env python3
"""Run Portacode repository creation from the active checkout when available."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _use_active_checkout() -> None:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        source_root = (codex_home / "portacode-source-root").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return
    if source_root and (Path(source_root) / "portacode" / "cli.py").is_file():
        sys.path.insert(0, source_root)


def main() -> None:
    _use_active_checkout()
    from portacode.cli import cli

    cli.main(args=["github-create", *sys.argv[1:]], prog_name="portacode")


if __name__ == "__main__":
    main()
