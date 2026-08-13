import json
from unittest import TestCase
from unittest.mock import patch

from portacode.connection.handlers.cloudflare_forwarding import (
    EXPOSED_SERVICES_JSON_PATH,
    _build_exposed_services_env_map,
    _sync_exposed_services_into_container,
    _build_ingress_entries,
    _new_container_hostname,
    _apply_and_persist_forwarding_rules,
)


class CloudflareForwardingEnvTests(TestCase):
    def test_new_container_hostname_uses_node_wildcard_namespace(self):
        state = {"domain": "example.com", "tunnel_name": "portacode-proxmox-6"}
        self.assertEqual(_new_container_hostname("598", 3001, "http", 0, state), "598.6.example.com")
        self.assertEqual(_new_container_hostname("598", 8000, "http", 1, state), "598-8000.6.example.com")
        self.assertEqual(
            _new_container_hostname("598", 8000, "https", 1, state),
            "598-8000-https.6.example.com",
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

    @patch("portacode.connection.handlers.cloudflare_forwarding.persist_forwarding_state", return_value={"updated_at": "now"})
    @patch("portacode.connection.handlers.cloudflare_forwarding._reload_cloudflared_service")
    @patch("portacode.connection.handlers.cloudflare_forwarding._route_dns")
    @patch("portacode.connection.handlers.cloudflare_forwarding._write_cloudflared_config")
    @patch("portacode.connection.handlers.cloudflare_forwarding._build_ingress_entries", return_value=[])
    @patch("portacode.connection.handlers.cloudflare_forwarding.load_forwarding_state")
    def test_wildcard_dns_is_only_routed_once(
        self, mock_load, _mock_build, _mock_write, mock_route, _mock_reload, _mock_persist
    ):
        tunnel = {"domain": "example.com", "tunnel_name": "portacode-proxmox-6"}
        mock_load.return_value = {"wildcard_dns_hostname": "*.6.example.com"}
        _apply_and_persist_forwarding_rules([], tunnel_state=tunnel)
        mock_route.assert_not_called()

        mock_load.return_value = {}
        _apply_and_persist_forwarding_rules([], tunnel_state=tunnel)
        mock_route.assert_called_once_with(["*.6.example.com"], "portacode-proxmox-6")

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
