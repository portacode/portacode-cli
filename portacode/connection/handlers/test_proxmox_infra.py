from unittest import TestCase
from unittest.mock import MagicMock, patch
import os

from portacode.connection.handlers.proxmox_infra import (
    RemoveProxmoxContainerHandler,
    _build_bootstrap_steps,
    _cacheable_bootstrap_steps,
    _dynamic_bootstrap_steps,
    _enforce_service_venv_execstart,
    _get_provisioning_user_info,
    _instantiate_container,
    _list_templates,
    _resolve_user_data_dir,
    _provisioning_cache_filename,
    _pinned_portacode_install_command,
    _sanitize_project_paths,
    _save_provisioning_cache,
)


class ProxmoxInfraHandlerTests(TestCase):
    @patch("portacode.connection.handlers.proxmox_infra._run_pct_check")
    def test_resolve_user_data_dir_uses_passwd_lookup_not_login_shell(self, mock_run_pct_check):
        mock_run_pct_check.return_value = {"stdout": "/root", "stderr": "", "returncode": 0}

        path = _resolve_user_data_dir(145, "root")

        self.assertEqual(path, "/root/.local/share")
        called_command = mock_run_pct_check.call_args[0][1]
        self.assertIn("getent passwd", called_command)
        self.assertNotIn("su - root", called_command)

    def test_get_provisioning_user_info_defaults_to_root_with_generated_password(self):
        user, password, ssh_key = _get_provisioning_user_info({})

        self.assertEqual(user, "root")
        self.assertTrue(password)
        self.assertEqual(ssh_key, "")

    def test_build_bootstrap_steps_includes_portacode_connect_by_default(self):
        steps = _build_bootstrap_steps("svcuser", "pass", "", include_portacode_connect=True)
        self.assertTrue(any(step.get("name") == "portacode_connect" for step in steps))

    def test_build_bootstrap_steps_exposes_portacode_globally_from_venv(self):
        steps = _build_bootstrap_steps("svcuser", "pass", "", include_portacode_connect=False)
        symlink_step = next(step for step in steps if step.get("name") == "ensure_global_portacode_cli")
        self.assertIn("/usr/local/bin/portacode", symlink_step["cmd"])
        self.assertIn("/opt/portacode-venv/bin/portacode", symlink_step["cmd"])

    def test_build_bootstrap_steps_skips_portacode_connect_when_requested(self):
        steps = _build_bootstrap_steps("svcuser", "pass", "", include_portacode_connect=False)
        self.assertFalse(any(step.get("name") == "portacode_connect" for step in steps))

    def test_cache_name_is_versioned_and_bound_to_exact_source(self):
        ubuntu = _provisioning_cache_filename("local:vztmpl/ubuntu-26.04.tar.zst")
        debian = _provisioning_cache_filename("local:vztmpl/debian-13.tar.zst")

        self.assertIn("portacode-ready", ubuntu)
        self.assertNotEqual(ubuntu, debian)
        self.assertTrue(ubuntu.endswith(".tar.zst"))

    def test_internal_ready_archives_are_hidden_from_source_template_list(self):
        client = MagicMock()
        client.nodes.return_value.storage.return_value.content.get.return_value = [
            {"content": "vztmpl", "volid": "local:vztmpl/ubuntu-26.04.tar.zst"},
            {"content": "vztmpl", "volid": "local:vztmpl/portacode-ready-ubuntu-cache.tar.zst"},
        ]

        templates = _list_templates(client, "pve", [{"storage": "local"}])

        self.assertEqual(templates, ["local:vztmpl/ubuntu-26.04.tar.zst"])

    def test_cacheable_steps_install_codex_and_playwright_only_where_supported(self):
        apt_names = {step["name"] for step in _cacheable_bootstrap_steps("apt")}
        apk_names = {step["name"] for step in _cacheable_bootstrap_steps("apk")}
        dnf_names = {step["name"] for step in _cacheable_bootstrap_steps("dnf")}
        zypper_names = {step["name"] for step in _cacheable_bootstrap_steps("zypper")}

        self.assertIn("install_codex_dependencies", apt_names)
        self.assertIn("install_codex_dependencies", apk_names)
        self.assertIn("install_playwright_chromium", apt_names)
        self.assertNotIn("install_codex_dependencies", dnf_names)
        self.assertNotIn("install_codex_dependencies", zypper_names)

    def test_cache_codex_install_does_not_depend_on_child_portacode_cli_version(self):
        for manager in ("apt", "apk"):
            step = next(
                step
                for step in _cacheable_bootstrap_steps(manager)
                if step["name"] == "install_codex_dependencies"
            )
            self.assertIn("npm install -g @openai/codex@latest", step["cmd"])
            self.assertNotIn("portacode prepare codex", step["cmd"])
            self.assertNotIn("--install-only", step["cmd"])

    @patch(
        "portacode.connection.handlers.proxmox_infra._running_portacode_version",
        return_value="1.5.21.dev7",
    )
    def test_guest_portacode_install_is_pinned_to_node_prerelease(self, _mock_version):
        command = _pinned_portacode_install_command()

        self.assertIn("portacode==1.5.21.dev7", command)
        self.assertNotEqual(command.rsplit(" ", 1)[-1], "portacode")

    @patch("portacode.connection.handlers.proxmox_infra._start_container")
    @patch("portacode.connection.handlers.proxmox_infra._stop_container")
    @patch("portacode.connection.handlers.proxmox_infra._sanitize_container_for_cache")
    @patch("portacode.connection.handlers.proxmox_infra._pvesm_path")
    @patch("portacode.connection.handlers.proxmox_infra._call_subprocess")
    def test_vzdump_staging_directory_is_traversable_by_mapped_lxc_user(
        self, mock_call, mock_path, _mock_sanitize, _mock_stop, _mock_start
    ):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "portacode-ready-test.tar.zst"
            mock_path.return_value = destination

            def fake_call(command, **kwargs):
                dump_dir = Path(command[command.index("--dumpdir") + 1])
                self.assertEqual(os.stat(dump_dir).st_mode & 0o777, 0o755)
                (dump_dir / "vzdump-lxc-123-test.tar.zst").write_bytes(b"archive")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_call.side_effect = fake_call

            _save_provisioning_cache(
                MagicMock(), "pve", 123, "local:vztmpl/alpine.tar.zst"
            )

            self.assertTrue(destination.is_file())

    def test_dynamic_steps_keep_identity_and_refresh_cli_out_of_cache(self):
        steps = _dynamic_bootstrap_steps("alice", "secret", "ssh-ed25519 AAA")
        names = [step["name"] for step in steps]

        self.assertIn("refresh_portacode", names)
        self.assertIn("user_exists", names)
        self.assertIn("set_password", names)
        self.assertIn("add_ssh_key", names)
        self.assertNotIn("install_playwright_chromium", names)

    @patch("portacode.connection.handlers.proxmox_infra._wait_for_task", return_value=({"exitstatus": "OK"}, 1.0))
    @patch("portacode.connection.handlers.proxmox_infra._run_with_timeout")
    def test_managed_create_never_passes_request_credentials_to_proxmox(
        self, mock_run_with_timeout, _mock_wait
    ):
        proxmox = MagicMock()
        proxmox.nodes.return_value.storage.get.return_value = [
            {"storage": "local-lvm", "type": "lvmthin"}
        ]
        mock_run_with_timeout.side_effect = lambda action, **kwargs: action()
        proxmox.nodes.return_value.lxc.create.return_value = "UPID:test"
        payload = {
            "storage": "local-lvm",
            "disk_gib": 8,
            "hostname": "test",
            "template": "local:vztmpl/ubuntu.tar.zst",
            "ram_mib": 1024,
            "cpus": 1,
            "net0": "name=eth0,bridge=vmbr1,ip=dhcp",
            "password": "must-not-enter-cache-source",
            "ssh_public_key": "ssh-ed25519 must-not-enter-cache-source",
            "defer_credentials": True,
        }

        _instantiate_container(proxmox, "pve", payload, vmid=123)

        create_kwargs = proxmox.nodes.return_value.lxc.create.call_args.kwargs
        self.assertIsNone(create_kwargs["password"])
        self.assertIsNone(create_kwargs["ssh_public_keys"])

    def test_sanitize_project_paths_keeps_child_relative_markers_raw(self):
        paths = _sanitize_project_paths(["~/.openclaw", "$HOME/app"])

        self.assertEqual(paths, ["~/.openclaw", "$HOME/app"])

    @patch("portacode.connection.handlers.proxmox_infra._run_pct")
    @patch("portacode.connection.handlers.proxmox_infra._resolve_user_data_home", return_value="/home/user/.local/share")
    def test_enforce_service_venv_execstart_shell_quotes_sed_script_with_project_paths(
        self,
        _mock_data_home,
        mock_run_pct,
    ):
        mock_run_pct.return_value = {"returncode": 0, "stdout": "", "stderr": ""}

        _enforce_service_venv_execstart(
            101,
            "root",
            runtime_user="user",
            project_paths=["$HOME/.openclaw", "$HOME/.openclaw/workspace"],
        )

        issued_command = mock_run_pct.call_args.args[1]
        self.assertIn("sed -i ", issued_command)
        self.assertIn("s#^ExecStart=.*#ExecStart=/opt/portacode-venv/bin/python -m portacode connect --non-interactive", issued_command)
        self.assertIn("'\"'\"'$$HOME/.openclaw'\"'\"'", issued_command)
        self.assertIn("'\"'\"'$$HOME/.openclaw/workspace'\"'\"'", issued_command)

    @patch("portacode.connection.handlers.proxmox_infra.get_infra_snapshot", return_value={})
    @patch("portacode.connection.handlers.proxmox_infra._remove_container_record")
    @patch("portacode.connection.handlers.proxmox_infra._ensure_container_managed")
    @patch("portacode.connection.handlers.proxmox_infra._read_container_record")
    @patch("portacode.connection.handlers.proxmox_infra._get_node_from_config", return_value="pve2")
    @patch("portacode.connection.handlers.proxmox_infra._connect_proxmox", return_value=object())
    @patch("portacode.connection.handlers.proxmox_infra._ensure_infra_configured", return_value={"token_value": "x"})
    @patch("portacode.connection.handlers.cloudflare_forwarding.set_container_forwarding_rules")
    def test_remove_container_succeeds_when_proxmox_container_already_missing(
        self,
        mock_clear_forwarding,
        _mock_configured,
        _mock_connect,
        _mock_get_node,
        mock_read_record,
        mock_ensure_managed,
        mock_remove_record,
        _mock_snapshot,
    ):
        mock_read_record.return_value = {"vmid": 134, "device_id": "42"}
        mock_ensure_managed.side_effect = RuntimeError(
            "500 Internal Server Error: Configuration file 'nodes/pve2/lxc/134.conf' does not exist"
        )

        handler = RemoveProxmoxContainerHandler(control_channel=MagicMock(), context={})
        response = handler.execute({"child_device_id": "42", "ctid": "134"})

        self.assertTrue(response["success"])
        self.assertEqual(response["status"], "deleted")
        self.assertEqual(response["ctid"], "134")
        self.assertIn("already deleted", response["message"])
        mock_clear_forwarding.assert_called_once_with("42", [])
        mock_remove_record.assert_called_once_with(134)
