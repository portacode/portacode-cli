from unittest import IsolatedAsyncioTestCase

from portacode.connection.guest_host_protocol import GUEST_HOST_REQUEST
from portacode.connection.handlers.base import SyncHandler
from portacode.connection.terminal import TerminalManager


class RecordingChannel:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


class ExposePortsHandler(SyncHandler):
    @property
    def command_name(self):
        return "configure_proxmox_container_expose_ports"

    def execute(self, message):
        return {"event": "proxmox_container_expose_ports_configured", "child_device_id": message["child_device_id"]}


class Registry:
    def __init__(self, handler):
        self.handler = handler

    def get_handler(self, command):
        return self.handler if command == self.handler.command_name else None


class CommandRecordingHandler(SyncHandler):
    def __init__(self, control_channel, session, command):
        super().__init__(control_channel, session)
        self._command = command
        self.messages = []

    @property
    def command_name(self):
        return self._command

    def execute(self, message):
        self.messages.append(message)
        return {"event": "handled", "command": self._command}


def request(request_id="ghr-1", target="42"):
    return {
        "type": GUEST_HOST_REQUEST,
        "command": "configure_proxmox_container_expose_ports",
        "request_id": request_id,
        "target_device_id": target,
        "authorization": {
            "principal_type": "user",
            "principal_id": "7",
            "principal_role": "owner",
            "operation": "expose_ports",
        },
        "payload": {"child_device_id": target, "expose_ports": [8000]},
    }


class GuestHostTerminalTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = TerminalManager.__new__(TerminalManager)
        self.manager._control_channel = RecordingChannel()
        self.manager._guest_host_request_results = {}
        self.manager._command_registry = Registry(ExposePortsHandler(self.manager._control_channel, {}))

    async def test_executes_new_envelope_without_changing_legacy_handler(self):
        await self.manager._handle_guest_host_request(request())
        ack, result = self.manager._control_channel.sent
        self.assertEqual(ack["type"], "guest_host_ack")
        self.assertTrue(ack["data"]["accepted"])
        self.assertEqual(result["type"], "guest_host_result")
        self.assertTrue(result["data"]["success"])
        self.assertEqual(result["on_behalf_of_device"], "42")
        self.assertEqual(result["data"]["response"]["child_device_id"], "42")

    async def test_replay_returns_cached_result_without_reexecuting(self):
        await self.manager._handle_guest_host_request(request())
        self.manager._control_channel.sent.clear()
        await self.manager._handle_guest_host_request(request())
        self.assertEqual(len(self.manager._control_channel.sent), 1)
        self.assertEqual(self.manager._control_channel.sent[0]["type"], "guest_host_result")

    async def test_mismatched_child_fails_closed_without_response(self):
        envelope = request()
        envelope["payload"]["child_device_id"] = "43"
        await self.manager._handle_guest_host_request(envelope)
        self.assertEqual(self.manager._control_channel.sent, [])

    async def test_enabled_request_without_registered_handler_is_rejected(self):
        envelope = request()
        envelope["command"] = "stop_proxmox_container"
        envelope["authorization"]["operation"] = "power.stop"
        await self.manager._handle_guest_host_request(envelope)
        ack, result = self.manager._control_channel.sent
        self.assertTrue(ack["data"]["accepted"])
        self.assertEqual(result["data"]["error_code"], "handler_unavailable")

    async def test_each_advertised_operation_delegates_to_existing_handler(self):
        cases = (
            ("start_proxmox_container", "power.start"),
            ("stop_proxmox_container", "power.stop"),
            ("remove_proxmox_container", "delete"),
            ("create_proxmox_container", "provision"),
        )
        for command, operation in cases:
            with self.subTest(command=command):
                handler = CommandRecordingHandler(self.manager._control_channel, {}, command)
                self.manager._command_registry = Registry(handler)
                self.manager._control_channel.sent.clear()
                envelope = request(request_id=f"ghr-{command}")
                envelope["command"] = command
                envelope["authorization"]["operation"] = operation

                await self.manager._handle_guest_host_request(envelope)

                ack, result = self.manager._control_channel.sent
                self.assertTrue(ack["data"]["accepted"])
                self.assertTrue(result["data"]["success"])
                self.assertEqual(len(handler.messages), 1)
                self.assertEqual(handler.messages[0]["child_device_id"], "42")
