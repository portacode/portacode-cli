from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from portacode.codex_prepare import (
    CodexPreparationError,
    _run,
    _install_node_if_needed,
    _install_codex,
    ensure_codex_home,
    install_codex_dependencies,
    resolve_codex_home,
    write_codex_config,
)


def test_install_codex_dependencies_is_cache_safe(monkeypatch):
    calls = []
    monkeypatch.setattr("portacode.codex_prepare._authorize_sudo_if_needed", lambda: calls.append("sudo"))
    monkeypatch.setattr("portacode.codex_prepare._install_node_if_needed", lambda: calls.append("node"))
    monkeypatch.setattr("portacode.codex_prepare._install_codex", lambda: calls.append("codex"))
    monkeypatch.setattr(
        "portacode.codex_prepare._verify_loopback_proxy",
        lambda: (_ for _ in ()).throw(AssertionError("install phase must not contact the proxy")),
    )

    install_codex_dependencies()

    assert calls == ["sudo", "node", "codex"]


def test_node_without_npm_is_not_treated_as_ready(monkeypatch):
    installed = {"nvm": False}
    monkeypatch.setattr("portacode.codex_prepare._node_major", lambda: 22)
    monkeypatch.setattr("portacode.codex_prepare.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "portacode.codex_prepare.Path.read_text",
        lambda self, **kwargs: "ID=ubuntu\n",
    )
    monkeypatch.setattr(
        "portacode.codex_prepare.shutil.which",
        lambda name: (
            "/usr/bin/npm"
            if name == "npm" and installed["nvm"]
            else None
            if name in {"npm", "npm.cmd"}
            else f"/usr/bin/{name}"
        ),
    )
    monkeypatch.setattr(
        "portacode.codex_prepare._install_node_with_nvm",
        lambda: installed.__setitem__("nvm", True),
    )

    _install_node_if_needed()

    assert installed["nvm"] is True


def test_nvm_node_bin_is_added_to_current_path(tmp_path, monkeypatch):
    from portacode.codex_prepare import _install_node_with_nvm

    home = tmp_path / "root"
    nvm_dir = home / ".nvm"
    nvm_dir.mkdir(parents=True)
    (nvm_dir / "nvm.sh").write_text("# nvm", encoding="utf-8")
    node = nvm_dir / "versions/node/v22.1.0/bin/node"
    node.parent.mkdir(parents=True)
    node.write_text("", encoding="utf-8")
    monkeypatch.setattr("portacode.codex_prepare.Path.home", lambda: home)
    monkeypatch.setattr(
        "portacode.codex_prepare.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"bash", "curl"} else None,
    )
    monkeypatch.setattr(
        "portacode.codex_prepare.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=f"{node}\n", stderr=""),
    )
    monkeypatch.setenv("PATH", "/bin:/usr/bin")

    _install_node_with_nvm()

    assert str(node.parent) == __import__("os").environ["PATH"].split(":")[0]


def test_nvm_resolves_lts_alias_after_install(tmp_path, monkeypatch):
    from portacode.codex_prepare import _install_node_with_nvm

    home = tmp_path / "root"
    nvm_dir = home / ".nvm"
    node = nvm_dir / "versions/node/v24.19.0/bin/node"
    node.parent.mkdir(parents=True)
    node.write_text("", encoding="utf-8")
    (nvm_dir / "nvm.sh").write_text(
        """
nvm() {
    if [ "$1" = install ] && [ "$2" = --lts ]; then return 0; fi
    if [ "$1" = alias ] && [ "$2" = default ] && [ "$3" = 'lts/*' ]; then return 0; fi
    if [ "$1" = which ] && [ "$2" = 'lts/*' ]; then printf '%s\\n' "$FAKE_NODE"; return 0; fi
    printf 'unexpected nvm arguments: %s\\n' "$*" >&2
    return 9
}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("portacode.codex_prepare.Path.home", lambda: home)
    monkeypatch.setattr(
        "portacode.codex_prepare.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"bash", "curl"} else None,
    )
    monkeypatch.setenv("FAKE_NODE", str(node))
    monkeypatch.setenv("PATH", "/bin:/usr/bin")

    _install_node_with_nvm()

    assert __import__("os").environ["PATH"].split(":")[0] == str(node.parent)


def test_install_codex_does_not_sudo_user_owned_nvm(tmp_path, monkeypatch):
    home = tmp_path / "root"
    npm = home / ".nvm/versions/node/v22/bin/npm"
    npm.parent.mkdir(parents=True)
    npm.write_text("", encoding="utf-8")
    commands = []
    installed = {"value": False}
    monkeypatch.setattr("portacode.codex_prepare.Path.home", lambda: home)
    monkeypatch.setattr(
        "portacode.codex_prepare._codex_path",
        lambda: str(npm.parent / "codex") if installed["value"] else None,
    )
    monkeypatch.setattr("portacode.codex_prepare._codex_works", lambda path: installed["value"])
    monkeypatch.setattr(
        "portacode.codex_prepare.shutil.which",
        lambda name: str(npm) if name == "npm" else None,
    )

    def fake_run(command, **kwargs):
        commands.append(list(command))
        installed["value"] = True

    monkeypatch.setattr("portacode.codex_prepare._run", fake_run)
    monkeypatch.setattr("portacode.codex_prepare.platform.system", lambda: "Linux")

    _install_codex()

    assert commands == [[str(npm), "install", "-g", "@openai/codex@latest"]]


def test_run_allows_proxmox_apt_update_exit_100(monkeypatch):
    """Same tolerance as ensure_cloudflared / ensure_pyyaml / proxmox_infra."""
    monkeypatch.setattr(
        "portacode.codex_prepare.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=100,
            stdout="",
            stderr="E: Failed to fetch https://enterprise.proxmox.com/debian/pve ... 401 Unauthorized",
        ),
    )
    _run(["apt-get", "update"], ok_returncodes=(0, 100))


def test_run_still_fails_on_unexpected_exit(monkeypatch):
    monkeypatch.setattr(
        "portacode.codex_prepare.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=100, stdout="", stderr="boom"),
    )
    with pytest.raises(CodexPreparationError, match="Command failed \\(100\\)"):
        _run(["apt-get", "update"])


def test_resolve_codex_home_remaps_root_when_runtime_user_differs(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/root/.codex")
    monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.get_default_runtime_user",
        lambda message=None: "bishoy",
    )
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.get_runtime_user_home",
        lambda message=None: "/home/bishoy",
    )
    assert resolve_codex_home() == Path("/home/bishoy/.codex")


def test_resolve_codex_home_keeps_explicit_non_root(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/custom/codex")
    monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.get_default_runtime_user",
        lambda message=None: "bishoy",
    )
    assert resolve_codex_home() == Path("/custom/codex")


def test_write_codex_config_forces_local_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.get_default_runtime_user",
        lambda message=None: "bishoy",
    )
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.chown_path_if_possible",
        lambda *args, **kwargs: None,
    )
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text(
        '[projects."/home/bishoy/souldesign_container"]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    path = write_codex_config(home)
    text = path.read_text(encoding="utf-8")
    assert 'model_provider = "portacode_proxy"' in text
    assert "127.0.0.1:61789" in text
    assert "supports_websockets = false" in text
    assert "openai_base_url" in text
    assert '[projects."/home/bishoy/souldesign_container"]' in text
    assert 'trust_level = "trusted"' in text


def test_ensure_codex_home_repairs_runtime_user_directory_ownership(tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    ownership_calls = []
    monkeypatch.setattr("portacode.codex_prepare.resolve_codex_home", lambda: home)
    monkeypatch.setattr("portacode.codex_prepare._persist_codex_home_env", lambda path: None)
    monkeypatch.setattr("portacode.codex_prepare.write_codex_config", lambda path: path / "config.toml")
    monkeypatch.setitem(
        ensure_codex_home.__globals__,
        "_repair_codex_home_ownership",
        lambda codex_home, sessions_dir: ownership_calls.append((codex_home, sessions_dir)),
    )

    assert ensure_codex_home() == home
    assert ownership_calls == [(home, home / "sessions")]
