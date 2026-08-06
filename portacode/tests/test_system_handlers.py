from portacode.connection.handlers.system_handlers import SystemInfoHandler


def test_system_info_includes_codex_installation_capability(monkeypatch):
    monkeypatch.setattr(
        "portacode.connection.handlers.system_handlers.CodexAppServer.get_binary_path",
        lambda: "/runtime/.local/bin/codex",
    )

    payload = SystemInfoHandler(None, {}).execute({})

    assert payload["event"] == "system_info"
    assert payload["info"]["codex"] == {
        "installed": True,
        "app_server_supported": True,
    }
