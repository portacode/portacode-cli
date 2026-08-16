import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from portacode.connection.handlers.cloudflare_forwarding import (
    EXPOSED_SERVICES_JSON_PATH,
    _build_exposed_services_env_map,
    _sync_exposed_services_into_container,
    _build_ingress_entries,
    _new_container_hostname,
    _apply_and_persist_forwarding_rules,
    _route_new_dns,
    _upsert_tunnel_dns_record,
    set_container_forwarding_rules,
)


class CloudflareForwardingEnvTests(TestCase):
    def test_new_container_hostname_stays_at_certificate_covered_level(self):
        state = {"domain": "example.com", "tunnel_name": "portacode-proxmox-6"}
        self.assertEqual(_new_container_hostname("598", 3001, "http", 0, state), "598.example.com")
        self.assertEqual(_new_container_hostname("598", 8000, "http", 1, state), "598-8000.example.com")
        self.assertEqual(
            _new_container_hostname("598", 8000, "https", 1, state),
            "598-8000-https.example.com",
        )

    @patch("portacode.connection.handlers.cloudflare_forwarding._load_leases", return_value=[])
    @patch("portacode.connection.handlers.cloudflare_forwarding._resolve_device_vmid", return_value=141)
    @patch("portacode.connection.handlers.cloudflare_forwarding._find_container_ip", side_effect=RuntimeError("offline"))
    def test_ingress_uses_recorded_ip_when_container_is_offline(
        self, _mock_find_ip, _mock_vmid, _mock_leases
    ):
        rules = [{
            "hostname": "598.6.example.com",
            "destination": "http://[598]:3001",
            "parsed": {"type": "device", "device_id": "598", "scheme": "http", "port": 3001, "path": ""},
            "resolved_ip": "10.77.0.25",
        }]
        entries = _build_ingress_entries(rules, object(), "pve2")
        self.assertEqual(entries[0]["service"], "http://10.77.0.25:3001")

    @patch("portacode.connection.handlers.cloudflare_forwarding._load_leases", return_value=[])
    @patch(
        "portacode.connection.handlers.cloudflare_forwarding._resolve_device_vmid",
        side_effect=RuntimeError("missing stale mapping"),
    )
    def test_ingress_uses_recorded_ip_when_neighbor_mapping_is_missing(
        self, _mock_vmid, _mock_leases
    ):
        rules = [{
            "hostname": "900.example.com",
            "destination": "http://[900]:8000",
            "parsed": {"type": "device", "device_id": "900", "scheme": "http", "port": 8000, "path": ""},
            "resolved_ip": "10.77.0.90",
        }]

        entries = _build_ingress_entries(rules, object(), "pve2")

        self.assertEqual(entries[0]["service"], "http://10.77.0.90:8000")

    @patch("portacode.connection.handlers.cloudflare_forwarding.persist_forwarding_state", return_value={"updated_at": "now"})
    @patch("portacode.connection.handlers.cloudflare_forwarding._reload_cloudflared_service")
    @patch("portacode.connection.handlers.cloudflare_forwarding._route_new_dns")
    @patch("portacode.connection.handlers.cloudflare_forwarding._write_cloudflared_config")
    @patch("portacode.connection.handlers.cloudflare_forwarding._build_ingress_entries")
    @patch("portacode.connection.handlers.cloudflare_forwarding.load_forwarding_state")
    def test_only_new_hostnames_are_routed(
        self, mock_load, mock_build, _mock_write, mock_route, _mock_reload, _mock_persist
    ):
        tunnel = {"domain": "example.com", "tunnel_name": "portacode-proxmox-6"}
        mock_load.return_value = {
            "rules": [{"hostname": "other-user.example.com", "destination": "http://[10]:80"}]
        }
        mock_build.return_value = [
            {"hostname": "other-user.example.com", "service": "http://10.0.0.1:80"},
            {"hostname": "new-user.example.com", "service": "http://10.0.0.2:80"},
        ]

        _apply_and_persist_forwarding_rules([], tunnel_state=tunnel)

        mock_route.assert_called_once_with(["new-user.example.com"], tunnel)

    @patch("portacode.connection.handlers.cloudflare_forwarding._cloudflare_api_request")
    def test_dns_upsert_reuses_matching_record(self, mock_request):
        mock_request.return_value = [
            {
                "id": "record-1",
                "type": "CNAME",
                "content": "tunnel-1.cfargotunnel.com",
                "proxied": True,
            }
        ]

        action = _upsert_tunnel_dns_record(
            "598.example.com",
            zone_id="zone-1",
            api_token="secret",
            tunnel_id="tunnel-1",
        )

        self.assertEqual(action, "unchanged")
        self.assertEqual(mock_request.call_count, 1)

    @patch("portacode.connection.handlers.cloudflare_forwarding._cloudflare_api_request")
    def test_dns_upsert_updates_conflicting_record(self, mock_request):
        mock_request.side_effect = [[{"id": "record-1", "type": "A", "content": "192.0.2.1"}], {}]

        action = _upsert_tunnel_dns_record(
            "598.example.com",
            zone_id="zone-1",
            api_token="secret",
            tunnel_id="tunnel-1",
        )

        self.assertEqual(action, "updated")
        self.assertEqual(mock_request.call_args_list[1].args[:2], ("PUT", "/zones/zone-1/dns_records/record-1"))

    @patch("portacode.connection.handlers.cloudflare_forwarding._route_dns")
    @patch("portacode.connection.handlers.cloudflare_forwarding._route_dns_via_api", side_effect=RuntimeError("no API"))
    def test_dns_api_failure_falls_back_to_cloudflared(self, _mock_api, mock_cli):
        tunnel = {"tunnel_name": "portacode-proxmox-6"}

        _route_new_dns(["598.example.com"], tunnel)

        mock_cli.assert_called_once_with(["598.example.com"], "portacode-proxmox-6")

    @patch("portacode.connection.handlers.cloudflare_forwarding._sync_exposed_services_into_container")
    @patch("portacode.connection.handlers.cloudflare_forwarding._get_node_from_config", return_value="pve2")
    @patch("portacode.connection.handlers.cloudflare_forwarding._connect_proxmox", return_value=object())
    @patch("portacode.connection.handlers.cloudflare_forwarding._ensure_infra_configured", return_value={})
    @patch("portacode.connection.handlers.cloudflare_forwarding._apply_and_persist_forwarding_rules")
    @patch("portacode.connection.handlers.cloudflare_forwarding.load_forwarding_state")
    @patch("portacode.connection.handlers.cloudflare_forwarding._load_tunnel_state")
    def test_container_update_preserves_neighbor_and_migrates_nested_hostname(
        self,
        mock_tunnel,
        mock_load,
        mock_apply,
        _mock_infra,
        _mock_connect,
        _mock_node,
        _mock_sync,
    ):
        mock_tunnel.return_value = {
            "domain": "example.com",
            "tunnel_name": "portacode-proxmox-6",
        }
        neighbor = {
            "hostname": "900.example.com",
            "destination": "http://[900]:8000",
            "parsed": {
                "type": "device",
                "device_id": "900",
                "scheme": "http",
                "port": 8000,
                "path": "",
            },
            "resolved_ip": "10.0.0.90",
        }
        old_target = {
            "hostname": "598.6.example.com",
            "destination": "http://[598]:3001",
            "parsed": {
                "type": "device",
                "device_id": "598",
                "scheme": "http",
                "port": 3001,
                "path": "",
            },
            "resolved_ip": "10.0.0.58",
        }
        mock_load.return_value = {"rules": [neighbor, old_target]}
        mock_apply.side_effect = lambda rules, **_kwargs: {
            "rules": rules,
            "updated_at": "now",
        }

        result = set_container_forwarding_rules(
            "598",
            [{"subdomain": "598", "port": 3001, "protocol": "http"}],
        )

        applied_rules = mock_apply.call_args.args[0]
        self.assertEqual(applied_rules[0], neighbor)
        self.assertEqual(applied_rules[1]["hostname"], "598.example.com")
        self.assertEqual(applied_rules[1]["resolved_ip"], "10.0.0.58")
        self.assertEqual(result["exposed_ports"][0]["hostname"], "598.example.com")

    @patch("portacode.connection.handlers.cloudflare_forwarding.persist_forwarding_state")
    @patch("portacode.connection.handlers.cloudflare_forwarding._reload_cloudflared_service")
    @patch("portacode.connection.handlers.cloudflare_forwarding._route_new_dns", side_effect=RuntimeError("DNS failed"))
    @patch("portacode.connection.handlers.cloudflare_forwarding._build_ingress_entries")
    @patch("portacode.connection.handlers.cloudflare_forwarding.load_forwarding_state", return_value={"rules": []})
    def test_failed_update_restores_previous_config(
        self, _mock_load, mock_build, _mock_route, mock_reload, mock_persist
    ):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yml"
            config_path.write_text("previous config\n", encoding="utf-8")
            mock_build.return_value = [{"hostname": "new.example.com", "service": "http://10.0.0.2"}]
            tunnel = {
                "domain": "example.com",
                "tunnel_name": "portacode-proxmox-6",
                "tunnel_id": "tunnel-1",
                "credentials_file": str(config_path),
                "config_path": str(config_path),
            }

            with self.assertRaisesRegex(RuntimeError, "DNS failed"):
                _apply_and_persist_forwarding_rules([], tunnel_state=tunnel)

            self.assertEqual(config_path.read_text(encoding="utf-8"), "previous config\n")
            mock_reload.assert_called_once()
            mock_persist.assert_not_called()

    def test_build_exposed_services_env_map_adds_indexed_public_host_aliases(self):
        env_map = _build_exposed_services_env_map(
            [
                {"hostname": "123.example.com", "url": "https://123.example.com", "port": 443},
                {"hostname": "1_123.example.com", "url": "https://1_123.example.com", "port": 12321},
                {"hostname": "2_123.example.com", "url": "https://2_123.example.com", "port": 12322},
            ]
        )

        self.assertEqual(env_map["PORTACODE_PUBLIC_HOST"], "123.example.com")
        self.assertEqual(env_map["PORTACODE_PUBLIC_HOST_1"], "123.example.com")
        self.assertEqual(env_map["PORTACODE_PUBLIC_HOST_2"], "1_123.example.com")
        self.assertEqual(env_map["PORTACODE_PUBLIC_HOST_3"], "2_123.example.com")

    @patch("portacode.connection.handlers.cloudflare_forwarding._push_root_file_to_container")
    @patch("portacode.connection.handlers.cloudflare_forwarding._resolve_device_vmid", return_value=321)
    def test_sync_exposed_services_into_container_pushes_only_canonical_json(
        self,
        _mock_resolve_vmid,
        mock_push,
    ):
        exposed_ports = [
            {"hostname": "123.example.com", "url": "https://123.example.com", "port": 443},
        ]

        _sync_exposed_services_into_container(
            container_device_id="123",
            exposed_ports=exposed_ports,
            proxmox=object(),
            node="pve",
        )

        mock_push.assert_called_once()
        vmid, path, data = mock_push.call_args.args[:3]
        self.assertEqual(vmid, 321)
        self.assertEqual(path, EXPOSED_SERVICES_JSON_PATH)
        self.assertEqual(
            json.loads(data.decode("utf-8")),
            {
                "device_id": "123",
                "exposed_services": exposed_ports,
            },
        )
        self.assertEqual(mock_push.call_args.kwargs["mode"], 0o644)
