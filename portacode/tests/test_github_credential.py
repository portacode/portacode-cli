import io
import json

from portacode import github_credential


def test_repository_parser_is_github_and_path_scoped():
    assert github_credential._repository_from_input(
        {"host": "github.com", "path": "owner/private-repo.git"}
    ) == "owner/private-repo"
    assert github_credential._repository_from_input(
        {"host": "example.com", "path": "owner/private-repo.git"}
    ) is None
    assert github_credential._repository_from_input(
        {"host": "github.com", "path": "owner"}
    ) is None


def test_get_credential_signs_exact_broker_request(monkeypatch):
    observed = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "username": "x-access-token", "password": "temporary"}

    def fake_post(url, *, content, headers, timeout):
        observed.update(url=url, content=content, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setattr(github_credential, "_signed_headers", lambda body, path: {"Signed": path})
    monkeypatch.setattr(github_credential.httpx, "post", fake_post)
    monkeypatch.setenv("PORTACODE_GITHUB_BASE_URL", "https://portacode.test")

    credential = github_credential.get_credential("owner/private")

    assert credential["password"] == "temporary"
    assert observed["url"] == "https://portacode.test/dashboard/github/device-credential/"
    assert json.loads(observed["content"]) == {"repository": "owner/private"}
    assert observed["headers"] == {"Signed": github_credential.BROKER_PATH}


def test_create_repository_signs_exact_device_request(monkeypatch):
    observed = {}

    class Response:
        status_code = 201

        @staticmethod
        def json():
            return {"ok": True, "full_name": "owner/new-repo", "clone_url": "https://github.com/owner/new-repo.git"}

    def fake_post(url, *, content, headers, timeout):
        observed.update(url=url, content=content, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setattr(github_credential, "_signed_headers", lambda body, path: {"Signed": path})
    monkeypatch.setattr(github_credential.httpx, "post", fake_post)
    monkeypatch.setenv("PORTACODE_GITHUB_BASE_URL", "https://portacode.test")

    result = github_credential.create_repository(account="owner", name="new-repo")

    assert result["full_name"] == "owner/new-repo"
    assert observed["url"].endswith(github_credential.CREATE_REPOSITORY_PATH)
    assert json.loads(observed["content"]) == {
        "account": "owner", "name": "new-repo", "private": True, "description": "",
    }
    assert observed["headers"] == {"Signed": github_credential.CREATE_REPOSITORY_PATH}


def test_helper_outputs_nothing_for_non_github_hosts(monkeypatch, capsys):
    monkeypatch.setattr(github_credential.sys, "stdin", io.StringIO("host=example.com\npath=o/r\n\n"))
    monkeypatch.setattr(github_credential.sys, "argv", ["git-credential-portacode", "get"])
    assert github_credential.main() == 0
    assert capsys.readouterr().out == ""


def test_helper_command_is_resolved_beside_portacode_python(monkeypatch, tmp_path):
    scripts = tmp_path / "venv" / "bin"
    scripts.mkdir(parents=True)
    python = scripts / "python"
    helper = scripts / "git-credential-portacode"
    python.touch()
    helper.touch()
    monkeypatch.setattr(github_credential.sys, "executable", str(python))

    assert github_credential._helper_command() == str(helper.resolve())


def test_helper_command_supports_source_checkout(monkeypatch, tmp_path):
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(github_credential.sys, "executable", str(python))
    monkeypatch.setattr(github_credential.shutil, "which", lambda _name: None)

    command = github_credential._helper_command()

    assert command.startswith("!env PYTHONPATH=")
    assert f"{python.resolve()} -m portacode.github_credential" in command


def test_configure_git_uses_absolute_helper_path(monkeypatch):
    observed = []
    helper = "/opt/portacode-venv/bin/git-credential-portacode"
    monkeypatch.setattr(github_credential, "_helper_command", lambda: helper)
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.get_default_runtime_user",
        lambda: "root",
    )
    monkeypatch.setattr(
        "portacode.connection.handlers.runtime_user.wrap_argv_for_user",
        lambda argv, _user: argv,
    )
    monkeypatch.setattr(
        github_credential.subprocess,
        "run",
        lambda argv, check: observed.append((argv, check)),
    )

    github_credential.configure_git()

    assert observed[0] == (
        [
            "git", "config", "--global", "--replace-all",
            "credential.https://github.com.helper", "",
        ],
        True,
    )
    assert observed[1] == (
        [
            "git", "config", "--global", "--add",
            "credential.https://github.com.helper", helper,
        ],
        True,
    )
    assert observed[2] == (
        ["git", "config", "--global", "credential.https://github.com.useHttpPath", "true"],
        True,
    )
