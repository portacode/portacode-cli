from unittest import TestCase

from portacode.connection.guest_host_protocol import (
    GUEST_HOST_ACK,
    GUEST_HOST_REQUEST,
    GuestHostProtocolError,
    build_guest_host_response,
    validate_guest_host_request,
)


def valid_request():
    return {
        "type": GUEST_HOST_REQUEST,
        "command": "stop_proxmox_container",
        "request_id": "req-1",
        "target_device_id": "42",
        "authorization": {
            "principal_type": "user",
            "principal_id": "7",
            "principal_role": "owner",
            "operation": "power.stop",
        },
        "payload": {"child_device_id": "42"},
    }


class GuestHostProtocolTests(TestCase):
    def test_resize_request_is_allowlisted(self):
        request = valid_request()
        request["command"] = "resize_proxmox_container"
        request["authorization"]["operation"] = "resize"
        request["payload"].update({"cpus": 1.5, "ram_mib": 2048, "disk_gib": 8})
        envelope = validate_guest_host_request(request)
        self.assertEqual(envelope["command"], "resize_proxmox_container")

    def test_validates_request_without_trusting_role_for_routing(self):
        envelope = validate_guest_host_request(valid_request())
        self.assertEqual(envelope["target_device_id"], "42")
        self.assertEqual(envelope["authorization"]["principal_role"], "owner")

    def test_rejects_child_target_mismatch(self):
        request = valid_request()
        request["payload"]["child_device_id"] = "43"
        with self.assertRaisesRegex(GuestHostProtocolError, "must match"):
            validate_guest_host_request(request)

    def test_rejects_unknown_command_and_principal_type(self):
        request = valid_request()
        request["command"] = "terminal_exec"
        with self.assertRaisesRegex(GuestHostProtocolError, "unsupported"):
            validate_guest_host_request(request)
        request = valid_request()
        request["authorization"]["principal_type"] = "host_owner_session"
        with self.assertRaisesRegex(GuestHostProtocolError, "principal_type"):
            validate_guest_host_request(request)

    def test_response_is_correlated_and_on_behalf_of_guest(self):
        response = build_guest_host_response(
            GUEST_HOST_ACK,
            request_id="req-1",
            command="stop_proxmox_container",
            target_device_id="42",
            data={"accepted": True},
        )
        self.assertEqual(response["request_id"], "req-1")
        self.assertEqual(response["on_behalf_of_device"], "42")
