from types import SimpleNamespace

from portacode.connection.handlers import session


def test_resolve_session_shell_uses_account_shell_without_service_shell(monkeypatch):
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(session, "_IS_WINDOWS", False)
    monkeypatch.setattr("pwd.getpwnam", lambda user: SimpleNamespace(pw_shell="/bin/zsh"))
    monkeypatch.setattr(session.os.path, "isfile", lambda path: path == "/bin/zsh")
    monkeypatch.setattr(session.os, "access", lambda path, mode: path == "/bin/zsh")

    assert session._resolve_session_shell(None, "meena") == "/bin/zsh"
    assert session._shell_argv_for_session("/bin/zsh") == ["/bin/zsh", "--login"]


def test_resolve_session_shell_preserves_explicit_request(monkeypatch):
    monkeypatch.setattr(session, "_IS_WINDOWS", False)

    assert session._resolve_session_shell("/opt/homebrew/bin/fish", "meena") == "/opt/homebrew/bin/fish"
