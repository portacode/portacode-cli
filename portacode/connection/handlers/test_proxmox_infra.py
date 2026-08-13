from unittest import TestCase
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from portacode.connection.handlers.proxmox_infra import (
    RemoveProxmoxContainerHandler,
    _build_full_container_summary,
    _compose_managed_containers_summary,
    _build_bootstrap_steps,
    _cacheable_bootstrap_steps,
    _cache_source_display_name,
    _cache_template_hostname,
    _claim_ctid_for_reservation,
    _dynamic_bootstrap_steps,
    _enforce_service_venv_execstart,
    _get_provisioning_user_info,
    _instantiate_container,
    _legacy_cache_archive_matches,
    _remove_legacy_cache_archives,
    _list_templates,
    _clone_native_template,
    _find_native_template,
    _garbage_collect_stale_native_templates,
    _native_template_dependents,
    _reconcile_native_template_registry,
    _resolve_user_data_dir,
    _pinned_portacode_install_command,
    _running_portacode_version_is_prerelease,
    _require_successful_proxmox_task,
    _reserve_container_resources,
    reconcile_managed_containers_inventory,
    _sanitize_project_paths,
)


class ProxmoxInfraHandlerTests(TestCase):
    def test_cache_template_names_are_human_readable_and_fixed_size(self):
        source = "local:vztmpl/alpine-3.17-default_20221129_amd64.tar.xz"

        self.assertEqual(_cache_source_display_name(source), "Alpine 3.17")
        self.assertEqual(
            _cache_template_hostname(source),
            "cache-alpine-3-17-v2026-08-09-6-4g",
        )

    def test_reconciled_external_deletion_is_not_reinserted_from_local_record(self):
        summary = _compose_managed_containers_summary(
            [{"vmid": 144, "device_id": "42", "ram_mib": 2048, "disk_gib": 14, "cpus": 1}],
            [],
            {
                "containers": [],
                "missing_containers": [{"vmid": "144", "device_id": "42"}],
                "allocated_ram_mib": 0,
                "allocated_disk_gib": 0,
                "allocated_cpu_share": 0,
            },
            {"ram_mib": 2048, "disk_gib": 14, "cpu_share": 1},
        )

        self.assertEqual(summary["containers"], [])
        self.assertEqual(summary["missing_containers"][0]["vmid"], "144")

    @patch("portacode.connection.handlers.proxmox_infra._build_full_container_summary")
    @patch("portacode.connection.handlers.proxmox_infra._load_config", return_value={"node": "pve"})
    def test_reconciliation_atomically_replaces_inventory_baseline(self, _config, build_summary):
        refreshed = {"inventory_updated_at": "new", "total_ram_mib": 2, "total_disk_gib": 3, "total_cpu_share": 1}
        build_summary.return_value = refreshed
        state = {
            "initialized": True, "base_summary": {"inventory_updated_at": "old"},
            "initial_totals": {}, "records": {"144": {"vmid": 144}},
            "pending": {"reservation": {"ram_mib": 1}}, "revision": 4,
        }
        with patch("portacode.connection.handlers.proxmox_infra._MANAGED_CONTAINERS_STATE", state):
            result = reconcile_managed_containers_inventory()

        self.assertTrue(result["refreshed"])
        self.assertIs(state["base_summary"], refreshed)
        self.assertIn("reservation", state["pending"])
        self.assertEqual(state["revision"], 4)

    @patch("portacode.connection.handlers.proxmox_infra._load_config", return_value={"node": "pve"})
    def test_reconciliation_discards_scan_when_managed_state_changes(self, _config):
        state = {
            "initialized": True, "base_summary": {"inventory_updated_at": "old"},
            "initial_totals": {}, "records": {"144": {"vmid": 144}},
            "pending": {}, "revision": 7,
        }

        def concurrent_scan(_records, _config):
            state["records"]["145"] = {"vmid": 145}
            state["revision"] += 1
            return {"inventory_updated_at": "stale scan"}

        with patch("portacode.connection.handlers.proxmox_infra._MANAGED_CONTAINERS_STATE", state), patch(
            "portacode.connection.handlers.proxmox_infra._build_full_container_summary",
            side_effect=concurrent_scan,
        ):
            result = reconcile_managed_containers_inventory()

        self.assertEqual(result, {"refreshed": False, "reason": "state_changed"})
        self.assertEqual(state["base_summary"]["inventory_updated_at"], "old")

    def test_cpu_can_be_oversubscribed_when_ram_and_disk_are_available(self):
        state = {
            "initialized": True,
            "base_summary": {
                "available_ram_mib": 4096,
                "available_disk_gib": 20,
                "available_cpu_share": 0,
                "allocated_ram_mib": 0,
                "allocated_disk_gib": 0,
                "allocated_cpu_share": 4,
            },
            "initial_totals": {"ram_mib": 0, "disk_gib": 0, "cpu_share": 0},
            "records": {},
            "pending": {},
        }
        with patch(
            "portacode.connection.handlers.proxmox_infra._MANAGED_CONTAINERS_STATE", state
        ):
            reservation_id = _reserve_container_resources(
                {"ram_mib": 1024, "disk_gib": 3, "cpus": 2},
                device_id="123",
                request_id="request",
            )

        self.assertIn(reservation_id, state["pending"])
        self.assertEqual(state["pending"][reservation_id]["cpu_share"], 2)

    @patch("portacode.connection.handlers.proxmox_infra._load_provisioning_templates")
    @patch("portacode.connection.handlers.proxmox_infra._get_node_from_config", return_value="pve")
    @patch("portacode.connection.handlers.proxmox_infra._connect_proxmox")
    def test_cached_system_info_inventory_includes_extended_fields_without_repeat_scan(
        self, mock_connect, _mock_node, mock_templates
    ):
        proxmox = MagicMock()
        node_api = proxmox.nodes.return_value
        node_api.lxc.get.return_value = [
            {"vmid": 144, "name": "managed", "status": "running"},
            {"vmid": 9000, "name": "cache", "status": "stopped"},
        ]
        node_api.qemu.get.return_value = []
        node_api.lxc.return_value.config.get.side_effect = [
            {
                "description": "portacode-managed:true;device_id=42;provisioning_id=abc",
                "rootfs": "local-lvm:vm-144-disk-0,size=32G",
                "memory": 2048,
                "cores": 2,
            },
            {
                "description": "portacode-cache:true;cache_id=v2;cache_source=sourcehash",
                "template": 1,
                "rootfs": "local-lvm:base-9000-disk-0,size=4G",
            },
        ]
        node_api.status.get.return_value = {
            "memory": {"total": 16 * 1024**3, "used": 8 * 1024**3},
            "cpuinfo": {"cores": 8},
        }
        node_api.storage.get.return_value = [
            {"storage": "local-lvm", "type": "lvmthin", "active": 1, "enabled": 1}
        ]
        node_api.storage.return_value.status.get.return_value = {
            "total": 100 * 1024**3, "used": 25 * 1024**3, "avail": 75 * 1024**3,
        }
        mock_connect.return_value = proxmox
        mock_templates.return_value = [{
            "vmid": 9000, "source_template": "ubuntu.tar.zst", "cache_id": "v2", "status": "ready",
        }]
        records = [
            {"vmid": 144, "device_id": "42", "hostname": "managed", "disk_gib": 32, "ram_mib": 2048, "cpus": 2},
            {"vmid": 155, "device_id": "43", "hostname": "missing", "disk_gib": 8, "ram_mib": 512, "cpus": 1},
        ]

        summary = _build_full_container_summary(
            records,
            {"token_value": "secret", "node": "pve", "default_storage": "local-lvm", "templates": []},
        )

        self.assertEqual(summary["inventory_schema_version"], 2)
        self.assertEqual(summary["containers"][0]["provisioning_id"], "abc")
        self.assertEqual(summary["templates"][0]["vmid"], "9000")
        self.assertEqual(summary["missing_containers"][0]["device_id"], "43")
        self.assertEqual(summary["storages"][0]["available_gib"], 75.0)

    @patch(
        "portacode.connection.handlers.proxmox_infra._running_portacode_version",
        return_value="1.5.28.dev4",
    )
    def test_dev_version_retains_failed_provisioning_container(self, _mock_version):
        self.assertTrue(_running_portacode_version_is_prerelease())

    @patch(
        "portacode.connection.handlers.proxmox_infra._running_portacode_version",
        return_value="1.5.28",
    )
    def test_stable_version_cleans_failed_provisioning_container(self, _mock_version):
        self.assertFalse(_running_portacode_version_is_prerelease())

    @patch("portacode.connection.handlers.proxmox_infra._allocate_vmid", return_value=144)
    def test_ctid_claim_discards_stale_record_when_proxmox_reports_id_free(self, mock_allocate):
        state = {
            "initialized": True,
            "base_summary": {},
            "initial_totals": {},
            "records": {"144": {"vmid": 144, "device_id": "deleted-device"}},
            "pending": {"request": {"ctid": None}},
        }
        with TemporaryDirectory() as root, patch(
            "portacode.connection.handlers.proxmox_infra.CONTAINERS_DIR", Path(root)
        ), patch(
            "portacode.connection.handlers.proxmox_infra._MANAGED_CONTAINERS_STATE", state
        ):
            record_path = Path(root) / "ct-144.json"
            record_path.write_text("{}")

            claimed = _claim_ctid_for_reservation("request", MagicMock())

        self.assertEqual(claimed, 144)
        self.assertFalse(record_path.exists())
        self.assertNotIn("144", state["records"])
        self.assertEqual(state["pending"]["request"]["ctid"], 144)
        mock_allocate.assert_called_once()

    @patch("portacode.connection.handlers.proxmox_infra._allocate_vmid", side_effect=[144, 145])
    def test_ctid_claim_selects_next_free_id_when_another_request_is_pending(self, mock_allocate):
        state = {
            "initialized": True,
            "base_summary": {},
            "initial_totals": {},
            "records": {},
            "pending": {
                "first": {"ctid": 144},
                "second": {"ctid": None},
            },
        }
        with patch(
            "portacode.connection.handlers.proxmox_infra._MANAGED_CONTAINERS_STATE", state
        ):
            claimed = _claim_ctid_for_reservation("second", MagicMock())

        self.assertEqual(claimed, 145)
        self.assertEqual(state["pending"]["second"]["ctid"], 145)
        self.assertEqual(mock_allocate.call_args_list[1].kwargs["vmid"], 145)

    def test_failed_proxmox_task_is_not_accepted_as_success(self):
        with self.assertRaisesRegex(RuntimeError, "container deletion task failed"):
            _require_successful_proxmox_task(
                {"status": "stopped", "exitstatus": "storage error"},
                "container deletion",
            )

    def test_successful_proxmox_task_is_accepted(self):
        _require_successful_proxmox_task(
            {"status": "stopped", "exitstatus": "OK"},
            "container deletion",
        )

    def test_delete_task_without_final_status_is_not_accepted(self):
        with self.assertRaisesRegex(RuntimeError, "no final exit status"):
            _require_successful_proxmox_task(
                {"status": "stopped"},
                "container deletion",
                require_exitstatus=True,
            )

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

    def test_all_package_manager_profiles_install_git_system_wide(self):
        for manager in ("apt", "dnf", "yum", "apk", "pacman", "zypper"):
            with self.subTest(manager=manager, path="cache"):
                install_step = next(
                    step
                    for step in _cacheable_bootstrap_steps(manager)
                    if step["name"] == "install_deps"
                )
                self.assertRegex(install_step["cmd"], r"(?:^|\s)git(?:\s|$)")

            with self.subTest(manager=manager, path="direct"):
                install_step = next(
                    step
                    for step in _build_bootstrap_steps(
                        "svcuser",
                        "pass",
                        "",
                        include_portacode_connect=False,
                        package_manager=manager,
                    )
                    if step["name"] == "install_deps"
                )
                self.assertRegex(install_step["cmd"], r"(?:^|\s)git(?:\s|$)")

    def test_legacy_cleanup_namespace_does_not_match_unrelated_backups(self):
        self.assertTrue(
            _legacy_cache_archive_matches(
                "local:vztmpl/portacode-ready-ubuntu-abc-0123456789ab-2026-08-07.4.tar.zst"
            )
        )
        self.assertFalse(
            _legacy_cache_archive_matches(
                "backup:vztmpl/vzdump-lxc-138-2026_08_07.tar.zst"
            )
        )
        self.assertFalse(
            _legacy_cache_archive_matches("local:vztmpl/portacode-important.tar.zst")
        )

    @patch("portacode.connection.handlers.proxmox_infra._call_subprocess")
    def test_legacy_cleanup_deletes_only_exact_portacode_cache_archives(self, mock_call):
        mock_call.return_value = MagicMock(returncode=0, stdout="", stderr="")
        proxmox = MagicMock()
        proxmox.nodes.return_value.storage.return_value.content.get.return_value = [
            {
                "content": "vztmpl",
                "volid": "local:vztmpl/portacode-ready-ubuntu-0123456789ab-2026-08-07.4.tar.zst",
            },
            {
                "content": "vztmpl",
                "volid": "local:vztmpl/vzdump-lxc-138-2026_08_07.tar.zst",
            },
            {"content": "backup", "volid": "local:backup/portacode-ready-important"},
        ]

        removed = _remove_legacy_cache_archives(
            proxmox, "pve", [{"storage": "local"}]
        )

        self.assertEqual(len(removed), 1)
        mock_call.assert_called_once_with(["pvesm", "free", removed[0]], check=False)

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
        self.assertIn("install_playwright_chromium", apk_names)
        self.assertNotIn("install_codex_dependencies", dnf_names)
        self.assertNotIn("install_codex_dependencies", zypper_names)

        apt_playwright = next(
            step for step in _cacheable_bootstrap_steps("apt")
            if step["name"] == "install_playwright_chromium"
        )["cmd"]
        apk_playwright = next(
            step for step in _cacheable_bootstrap_steps("apk")
            if step["name"] == "install_playwright_chromium"
        )["cmd"]
        self.assertIn("npm install --prefix /opt/portacode-playwright playwright@latest", apt_playwright)
        self.assertIn('/opt/portacode-playwright/node_modules/playwright/cli.js', apt_playwright)
        self.assertIn('node "$playwright_cli" install --with-deps chromium', apt_playwright)
        self.assertIn('node "$playwright_cli" screenshot', apt_playwright)
        self.assertIn("apk add --no-cache chromium", apk_playwright)
        self.assertIn("/usr/bin/chromium-browser", apk_playwright)
        self.assertIn("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install --ignore-scripts", apk_playwright)
        self.assertIn("--prefix /opt/portacode-playwright playwright@latest", apk_playwright)
        self.assertIn('/opt/portacode-playwright/node_modules/playwright/cli.js', apk_playwright)
        self.assertIn("require('playwright')", apk_playwright)
        self.assertIn("executablePath:'/usr/bin/chromium-browser'", apk_playwright)
        self.assertIn("p.screenshot({path:'/tmp/portacode-playwright-smoke.png'})", apk_playwright)

    def test_legacy_alpine_is_upgraded_before_python_venv_is_created(self):
        steps = _cacheable_bootstrap_steps("apk")
        names = [step["name"] for step in steps]

        self.assertLess(names.index("upgrade_legacy_alpine"), names.index("create_portacode_venv"))
        upgrade = next(step for step in steps if step["name"] == "upgrade_legacy_alpine")
        self.assertIn("apk upgrade --available", upgrade["cmd"])

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

    @patch(
        "portacode.connection.handlers.proxmox_infra._wait_for_task",
        return_value=({"exitstatus": "OK"}, 0.1),
    )
    def test_native_cache_uses_linked_clone_and_records_parent_marker(self, _mock_wait):
        proxmox = MagicMock()
        proxmox.nodes.return_value.lxc.return_value.clone.post.return_value = "UPID:clone"
        payload = {
            "hostname": "child",
            "storage": "local-lvm",
            "disk_gib": 8,
            "cache_template_disk_gib": 8,
            "ram_mib": 1024,
            "swap_mb": 0,
            "cores": 1,
            "cpus": 0.5,
            "cpulimit": 0.5,
            "net0": "name=eth0,bridge=vmbr1,ip=dhcp",
            "description": "portacode-managed:true",
            "device_id": "1069",
            "provisioning_id": "abc",
        }

        _clone_native_template(proxmox, "pve", 900, 139, payload)

        clone_kwargs = proxmox.nodes.return_value.lxc.return_value.clone.post.call_args.kwargs
        self.assertEqual(clone_kwargs["full"], 0)
        self.assertNotIn("storage", clone_kwargs)
        config_kwargs = proxmox.nodes.return_value.lxc.return_value.config.put.call_args.kwargs
        self.assertIn("cache_parent=900", config_kwargs["description"])

    @patch("portacode.connection.handlers.proxmox_infra._load_managed_container_records")
    def test_template_dependents_ignore_records_from_other_nodes(self, mock_records):
        mock_records.return_value = [
            {"node": "other", "vmid": 201, "cache_template_vmid": 900}
        ]
        proxmox = MagicMock()
        proxmox.nodes.return_value.lxc.get.return_value = []

        self.assertEqual(_native_template_dependents(proxmox, "pve", 900), set())

    @patch("portacode.connection.handlers.proxmox_infra._proxmox_template_exists")
    @patch("portacode.connection.handlers.proxmox_infra._load_provisioning_templates")
    def test_latest_current_template_is_selected_for_exact_lineage(
        self, mock_load, mock_exists
    ):
        from portacode.connection.handlers.proxmox_infra import (
            PROVISIONING_CACHE_ID,
            _cache_lineage_key,
        )

        source = "local:vztmpl/ubuntu-26.04.tar.zst"
        lineage = _cache_lineage_key(source, "local-lvm")
        mock_load.return_value = [
            {"vmid": 900, "node": "pve", "lineage": lineage, "disk_gib": 4, "cache_id": PROVISIONING_CACHE_ID, "status": "ready", "created_at": "2026-08-07T01:00:00Z"},
            {"vmid": 901, "node": "pve", "lineage": lineage, "disk_gib": 4, "cache_id": PROVISIONING_CACHE_ID, "status": "ready", "created_at": "2026-08-07T02:00:00Z"},
            {"vmid": 902, "node": "pve", "lineage": lineage, "disk_gib": 4, "cache_id": "old", "status": "ready", "created_at": "2026-08-07T03:00:00Z"},
        ]
        mock_exists.return_value = True

        selected = _find_native_template(
            MagicMock(), "pve", source, "local-lvm", 8
        )

        self.assertEqual(selected["vmid"], 901)

    @patch("portacode.connection.handlers.proxmox_infra._proxmox_template_exists")
    @patch("portacode.connection.handlers.proxmox_infra._load_provisioning_templates")
    def test_noncanonical_cache_disk_is_not_selected(self, mock_load, mock_exists):
        from portacode.connection.handlers.proxmox_infra import (
            PROVISIONING_CACHE_ID,
            _cache_lineage_key,
        )

        source = "local:vztmpl/alpine-3.17-default_20221129_amd64.tar.xz"
        mock_load.return_value = [{
            "vmid": 900,
            "node": "pve",
            "lineage": _cache_lineage_key(source, "local-lvm"),
            "disk_gib": 3,
            "cache_id": PROVISIONING_CACHE_ID,
            "status": "ready",
        }]

        self.assertIsNone(_find_native_template(MagicMock(), "pve", source, "local-lvm", 8))
        mock_exists.assert_not_called()

    @patch(
        "portacode.connection.handlers.proxmox_infra._wait_for_task",
        side_effect=[
            ({"exitstatus": "OK"}, 0.1),
            ({"exitstatus": "OK"}, 0.1),
        ],
    )
    def test_linked_clone_is_expanded_to_requested_disk_size(self, _mock_wait):
        proxmox = MagicMock()
        proxmox.nodes.return_value.lxc.return_value.clone.post.return_value = "UPID:clone"
        proxmox.nodes.return_value.lxc.return_value.resize.put.return_value = "UPID:resize"
        payload = {
            "hostname": "child",
            "storage": "local-lvm",
            "disk_gib": 10,
            "cache_template_disk_gib": 4,
            "ram_mib": 1024,
            "swap_mb": 0,
            "cores": 1,
            "cpus": 1,
            "net0": "name=eth0,bridge=vmbr1,ip=dhcp",
            "description": "portacode-managed:true",
        }

        _clone_native_template(proxmox, "pve", 900, 139, payload)

        proxmox.nodes.return_value.lxc.return_value.resize.put.assert_called_once_with(
            disk="rootfs", size="10G"
        )

    @patch("portacode.connection.handlers.proxmox_infra._proxmox_template_exists")
    @patch("portacode.connection.handlers.proxmox_infra._load_provisioning_templates")
    def test_template_larger_than_request_is_not_selected(
        self, mock_load, mock_exists
    ):
        from portacode.connection.handlers.proxmox_infra import (
            PROVISIONING_CACHE_ID,
            _cache_lineage_key,
        )

        source = "local:vztmpl/ubuntu-26.04.tar.zst"
        mock_load.return_value = [{
            "vmid": 900,
            "node": "pve",
            "lineage": _cache_lineage_key(source, "local-lvm"),
            "disk_gib": 7,
            "cache_id": PROVISIONING_CACHE_ID,
            "status": "ready",
        }]

        selected = _find_native_template(
            MagicMock(), "pve", source, "local-lvm", 4
        )

        self.assertIsNone(selected)
        mock_exists.assert_not_called()

    def _registry_path(self, root: str) -> Path:
        return Path(root) / "provisioning_templates.json"

    @patch("portacode.connection.handlers.proxmox_infra._call_subprocess")
    @patch("portacode.connection.handlers.proxmox_infra._native_template_dependents")
    @patch("portacode.connection.handlers.proxmox_infra._proxmox_template_exists")
    def test_stale_template_is_retained_until_dependents_are_gone(
        self, mock_exists, mock_dependents, mock_call
    ):
        from portacode.connection.handlers.proxmox_infra import PROVISIONING_CACHE_ID
        import json

        mock_exists.return_value = True
        mock_dependents.return_value = {321}
        with TemporaryDirectory() as root, patch(
            "portacode.connection.handlers.proxmox_infra.PROVISIONING_TEMPLATES_PATH",
            self._registry_path(root),
        ):
            records = [
                {"vmid": 900, "node": "pve", "lineage": "lineage", "cache_id": "old", "status": "stale"},
                {"vmid": 901, "node": "pve", "lineage": "lineage", "cache_id": PROVISIONING_CACHE_ID, "status": "ready"},
            ]
            self._registry_path(root).write_text(json.dumps({"templates": records}))

            self.assertEqual(
                _garbage_collect_stale_native_templates(MagicMock(), "pve"), []
            )
            saved = json.loads(self._registry_path(root).read_text())["templates"]

        self.assertEqual(saved[0]["dependent_vmids"], [321])
        mock_call.assert_not_called()

    @patch("portacode.connection.handlers.proxmox_infra._call_subprocess")
    @patch("portacode.connection.handlers.proxmox_infra._native_template_dependents")
    @patch("portacode.connection.handlers.proxmox_infra._proxmox_template_exists")
    def test_stale_template_is_deleted_only_with_ready_replacement_and_no_dependents(
        self, mock_exists, mock_dependents, mock_call
    ):
        from portacode.connection.handlers.proxmox_infra import PROVISIONING_CACHE_ID
        import json

        mock_exists.return_value = True
        mock_dependents.return_value = set()
        mock_call.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with TemporaryDirectory() as root, patch(
            "portacode.connection.handlers.proxmox_infra.PROVISIONING_TEMPLATES_PATH",
            self._registry_path(root),
        ):
            records = [
                {"vmid": 900, "node": "pve", "lineage": "lineage", "cache_id": "old", "status": "stale"},
                {"vmid": 901, "node": "pve", "lineage": "lineage", "cache_id": PROVISIONING_CACHE_ID, "status": "ready"},
            ]
            self._registry_path(root).write_text(json.dumps({"templates": records}))

            removed = _garbage_collect_stale_native_templates(MagicMock(), "pve")
            saved = json.loads(self._registry_path(root).read_text())["templates"]

        self.assertEqual(removed, [900])
        self.assertEqual([record["vmid"] for record in saved], [901])
        mock_call.assert_called_once_with(
            ["pct", "destroy", "900", "--purge", "1"], check=False
        )

    @patch("portacode.connection.handlers.proxmox_infra._call_subprocess")
    @patch("portacode.connection.handlers.proxmox_infra._native_template_dependents")
    @patch("portacode.connection.handlers.proxmox_infra._proxmox_template_exists")
    def test_stale_template_without_replacement_is_never_deleted(
        self, mock_exists, mock_dependents, mock_call
    ):
        import json

        mock_exists.return_value = True
        with TemporaryDirectory() as root, patch(
            "portacode.connection.handlers.proxmox_infra.PROVISIONING_TEMPLATES_PATH",
            self._registry_path(root),
        ):
            records = [
                {"vmid": 900, "node": "pve", "lineage": "lineage", "cache_id": "old", "status": "stale"}
            ]
            self._registry_path(root).write_text(json.dumps({"templates": records}))

            self.assertEqual(
                _garbage_collect_stale_native_templates(MagicMock(), "pve"), []
            )

        mock_dependents.assert_not_called()
        mock_call.assert_not_called()

    @patch("portacode.connection.handlers.proxmox_infra._call_subprocess")
    @patch("portacode.connection.handlers.proxmox_infra._native_template_dependents")
    @patch("portacode.connection.handlers.proxmox_infra._proxmox_template_exists")
    def test_dependency_inspection_failure_fails_closed(
        self, mock_exists, mock_dependents, mock_call
    ):
        from portacode.connection.handlers.proxmox_infra import PROVISIONING_CACHE_ID
        import json

        mock_exists.return_value = True
        mock_dependents.return_value = None
        with TemporaryDirectory() as root, patch(
            "portacode.connection.handlers.proxmox_infra.PROVISIONING_TEMPLATES_PATH",
            self._registry_path(root),
        ):
            records = [
                {"vmid": 900, "node": "pve", "lineage": "lineage", "cache_id": "old", "status": "stale"},
                {"vmid": 901, "node": "pve", "lineage": "lineage", "cache_id": PROVISIONING_CACHE_ID, "status": "ready"},
            ]
            self._registry_path(root).write_text(json.dumps({"templates": records}))

            self.assertEqual(
                _garbage_collect_stale_native_templates(MagicMock(), "pve"), []
            )

        mock_call.assert_not_called()

    @patch("portacode.connection.handlers.proxmox_infra._call_subprocess")
    @patch("portacode.connection.handlers.proxmox_infra._native_template_dependents")
    @patch("portacode.connection.handlers.proxmox_infra._proxmox_template_exists")
    def test_proxmox_destroy_refusal_retains_stale_template_record(
        self, mock_exists, mock_dependents, mock_call
    ):
        from portacode.connection.handlers.proxmox_infra import PROVISIONING_CACHE_ID
        import json

        mock_exists.return_value = True
        mock_dependents.return_value = set()
        mock_call.return_value = MagicMock(
            returncode=255, stdout="", stderr="linked clone exists"
        )
        with TemporaryDirectory() as root, patch(
            "portacode.connection.handlers.proxmox_infra.PROVISIONING_TEMPLATES_PATH",
            self._registry_path(root),
        ):
            records = [
                {"vmid": 900, "node": "pve", "lineage": "lineage", "cache_id": "old", "status": "stale"},
                {"vmid": 901, "node": "pve", "lineage": "lineage", "cache_id": PROVISIONING_CACHE_ID, "status": "ready"},
            ]
            self._registry_path(root).write_text(json.dumps({"templates": records}))

            self.assertEqual(
                _garbage_collect_stale_native_templates(MagicMock(), "pve"), []
            )
            saved = json.loads(self._registry_path(root).read_text())["templates"]

        stale = next(record for record in saved if record["vmid"] == 900)
        self.assertIn("linked clone exists", stale["cleanup_error"])

    def test_registry_recovery_adopts_only_exact_owned_templates(self):
        import json

        proxmox = MagicMock()
        proxmox.nodes.return_value.lxc.get.return_value = [
            {"vmid": 900}, {"vmid": 901}, {"vmid": 902}
        ]
        configs = {
            "900": {"template": 1, "description": "portacode-cache:true;cache_id=current;cache_source=hash;storage=local-lvm;disk_gib=8"},
            "901": {"template": 1, "description": "note=portacode-cache:true-ish;cache_source=hash;storage=local-lvm;disk_gib=8"},
            "902": {"template": 0, "description": "portacode-cache:true;cache_source=hash;storage=local-lvm;disk_gib=8"},
        }
        proxmox.nodes.return_value.lxc.side_effect = lambda vmid=None: MagicMock(
            config=MagicMock(get=MagicMock(return_value=configs[str(vmid)]))
        )
        with TemporaryDirectory() as root, patch(
            "portacode.connection.handlers.proxmox_infra.PROVISIONING_TEMPLATES_PATH",
            self._registry_path(root),
        ), patch(
            "portacode.connection.handlers.proxmox_infra._cache_source_hash",
            return_value="hash",
        ):
            records = _reconcile_native_template_registry(
                proxmox, "pve", ["local:vztmpl/ubuntu.tar.zst"]
            )
            saved = json.loads(self._registry_path(root).read_text())["templates"]

        self.assertEqual([record["vmid"] for record in records], [900])
        self.assertEqual([record["vmid"] for record in saved], [900])

    def test_registry_migrates_disk_specific_lineage_and_marks_old_generation_stale(self):
        from portacode.connection.handlers.proxmox_infra import _cache_lineage_key
        import json

        source = "local:vztmpl/ubuntu.tar.zst"
        proxmox = MagicMock()
        proxmox.nodes.return_value.lxc.get.side_effect = RuntimeError("offline")
        with TemporaryDirectory() as root, patch(
            "portacode.connection.handlers.proxmox_infra.PROVISIONING_TEMPLATES_PATH",
            self._registry_path(root),
        ):
            self._registry_path(root).write_text(json.dumps({"templates": [{
                "vmid": 900,
                "node": "pve",
                "source_template": source,
                "storage": "local-lvm",
                "disk_gib": 7,
                "lineage": "old-source:local-lvm:7",
                "cache_id": "2026-08-07.5",
                "status": "ready",
            }]}))

            _reconcile_native_template_registry(proxmox, "pve", [source])
            saved = json.loads(self._registry_path(root).read_text())["templates"][0]

        self.assertEqual(saved["lineage"], _cache_lineage_key(source, "local-lvm"))
        self.assertEqual(saved["status"], "stale")

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

    @patch("portacode.connection.handlers.proxmox_infra._mark_device_deletion_requested")
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
        mock_mark_deletion,
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
        mock_mark_deletion.assert_called_once_with("42")

    @patch("portacode.connection.handlers.proxmox_infra._mark_device_deletion_requested")
    @patch("portacode.connection.handlers.proxmox_infra.get_infra_snapshot", return_value={})
    @patch("portacode.connection.handlers.proxmox_infra._remove_container_records_for_device", return_value=[])
    @patch(
        "portacode.connection.handlers.proxmox_infra._resolve_vmid_for_device_in_proxmox",
        side_effect=RuntimeError("No managed container found"),
    )
    @patch(
        "portacode.connection.handlers.proxmox_infra._resolve_vmid_for_device",
        side_effect=RuntimeError("No local record"),
    )
    @patch("portacode.connection.handlers.proxmox_infra._get_node_from_config", return_value="pve2")
    @patch("portacode.connection.handlers.proxmox_infra._connect_proxmox", return_value=object())
    @patch("portacode.connection.handlers.proxmox_infra._ensure_infra_configured", return_value={"token_value": "x"})
    def test_remove_container_succeeds_when_provisioning_never_created_a_container(
        self,
        _mock_configured,
        _mock_connect,
        _mock_get_node,
        _mock_local_lookup,
        _mock_proxmox_lookup,
        mock_remove_records,
        _mock_snapshot,
        mock_mark_deletion,
    ):
        from portacode.connection.handlers.proxmox_infra import _DeviceLookupError

        _mock_local_lookup.side_effect = _DeviceLookupError("No local record")
        _mock_proxmox_lookup.side_effect = _DeviceLookupError("No managed container found")
        handler = RemoveProxmoxContainerHandler(control_channel=MagicMock(), context={})

        response = handler.execute({"child_device_id": "42"})

        self.assertTrue(response["success"])
        self.assertEqual(response["status"], "deleted")
        self.assertIsNone(response["ctid"])
        mock_mark_deletion.assert_called_once_with("42")
        mock_remove_records.assert_called_once_with("42")

    @patch("portacode.connection.handlers.proxmox_infra._mark_device_deletion_requested")
    @patch("portacode.connection.handlers.proxmox_infra._delete_container")
    @patch("portacode.connection.handlers.proxmox_infra._stop_container")
    @patch("portacode.connection.handlers.proxmox_infra._remove_container_record")
    @patch("portacode.connection.handlers.proxmox_infra._ensure_container_managed")
    @patch("portacode.connection.handlers.proxmox_infra._read_container_record")
    @patch("portacode.connection.handlers.proxmox_infra._get_node_from_config", return_value="pve2")
    @patch("portacode.connection.handlers.proxmox_infra._connect_proxmox", return_value=object())
    @patch("portacode.connection.handlers.proxmox_infra._ensure_infra_configured", return_value={"token_value": "x"})
    @patch("portacode.connection.handlers.cloudflare_forwarding.set_container_forwarding_rules")
    def test_remove_container_preserves_metadata_when_proxmox_delete_task_fails(
        self,
        _mock_clear_forwarding,
        _mock_configured,
        _mock_connect,
        _mock_get_node,
        mock_read_record,
        _mock_ensure_managed,
        mock_remove_record,
        mock_stop,
        mock_delete,
        _mock_mark_deletion,
    ):
        mock_read_record.return_value = {"vmid": 134, "device_id": "42"}
        mock_stop.return_value = ({"status": "stopped", "exitstatus": "OK"}, 0.1)
        mock_delete.return_value = (
            {"status": "stopped", "exitstatus": "storage error"},
            0.1,
        )

        handler = RemoveProxmoxContainerHandler(control_channel=MagicMock(), context={})
        with self.assertRaisesRegex(RuntimeError, "container deletion task failed"):
            handler.execute({"child_device_id": "42", "ctid": "134"})

        mock_remove_record.assert_not_called()
