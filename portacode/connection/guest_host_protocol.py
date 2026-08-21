"""Fail-closed protocol contract for Guest Host Requests.

The transport is intentionally separate from ordinary client-session messages.
This module contains no dispatch logic, so introducing it does not change live
message handling until a request type is explicitly enabled and registered.
"""

from __future__ import annotations

from typing import Any, Mapping


GUEST_HOST_REQUEST = "guest_host_request"
GUEST_HOST_ACK = "guest_host_ack"
GUEST_HOST_PROGRESS = "guest_host_progress"
GUEST_HOST_RESULT = "guest_host_result"

GUEST_HOST_MESSAGE_TYPES = frozenset(
    {GUEST_HOST_REQUEST, GUEST_HOST_ACK, GUEST_HOST_PROGRESS, GUEST_HOST_RESULT}
)

GUEST_HOST_REQUEST_COMMANDS = frozenset(
    {
        "configure_proxmox_container_expose_ports",
        "start_proxmox_container",
        "stop_proxmox_container",
        "restart_proxmox_container",
        "remove_proxmox_container",
        "create_proxmox_container",
        "resize_proxmox_container",
    }
)
GUEST_HOST_COMMAND_OPERATIONS = {
    "configure_proxmox_container_expose_ports": "expose_ports",
    "start_proxmox_container": "power",
    "stop_proxmox_container": "power",
    "restart_proxmox_container": "power",
    "remove_proxmox_container": "delete",
    "create_proxmox_container": "provision",
    "resize_proxmox_container": "resize",
}

# Every advertised operation is mapped to its existing, independently
# registered legacy handler.  Server-side capability and runtime gates still
# control which operations are actually sent through this protocol.
GUEST_HOST_EXECUTABLE_COMMANDS = frozenset(
    {
        "configure_proxmox_container_expose_ports",
        "start_proxmox_container",
        "stop_proxmox_container",
        "restart_proxmox_container",
        "remove_proxmox_container",
        "create_proxmox_container",
        "resize_proxmox_container",
    }
)


class GuestHostProtocolError(ValueError):
    """Raised when a Guest Host Request envelope violates the contract."""


def _required_text(envelope: Mapping[str, Any], field: str) -> str:
    value = envelope.get(field)
    if not isinstance(value, str) or not value.strip():
        raise GuestHostProtocolError(f"{field} must be a non-empty string")
    return value.strip()


def validate_guest_host_request(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize an inbound Guest Host Request envelope."""

    if not isinstance(envelope, Mapping):
        raise GuestHostProtocolError("Guest Host Request must be an object")
    if envelope.get("type") != GUEST_HOST_REQUEST:
        raise GuestHostProtocolError(f"type must be {GUEST_HOST_REQUEST!r}")

    command = _required_text(envelope, "command")
    if command not in GUEST_HOST_REQUEST_COMMANDS:
        raise GuestHostProtocolError(f"unsupported Guest Host Request command: {command}")
    request_id = _required_text(envelope, "request_id")
    target_device_id = _required_text(envelope, "target_device_id")
    if not target_device_id.isdigit():
        raise GuestHostProtocolError("target_device_id must contain a numeric device ID")

    authorization = envelope.get("authorization")
    if not isinstance(authorization, Mapping):
        raise GuestHostProtocolError("authorization must be an object")
    principal_type = _required_text(authorization, "principal_type")
    if principal_type not in {"user", "server_worker", "scheduled_automation", "system"}:
        raise GuestHostProtocolError(f"unsupported principal_type: {principal_type}")
    _required_text(authorization, "principal_id")
    operation = _required_text(authorization, "operation")
    expected_operation = GUEST_HOST_COMMAND_OPERATIONS[command]
    if operation != expected_operation and not operation.startswith(f"{expected_operation}."):
        raise GuestHostProtocolError("authorization.operation does not match command")

    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        raise GuestHostProtocolError("payload must be an object")
    child_device_id = str(payload.get("child_device_id") or "").strip()
    if child_device_id != target_device_id:
        raise GuestHostProtocolError("payload.child_device_id must match target_device_id")

    normalized = dict(envelope)
    normalized["command"] = command
    normalized["request_id"] = request_id
    normalized["target_device_id"] = target_device_id
    normalized["authorization"] = dict(authorization)
    normalized["payload"] = dict(payload)
    return normalized


def build_guest_host_response(
    message_type: str,
    *,
    request_id: str,
    command: str,
    target_device_id: str,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a correlated ack/progress/result envelope."""

    if message_type not in {GUEST_HOST_ACK, GUEST_HOST_PROGRESS, GUEST_HOST_RESULT}:
        raise GuestHostProtocolError("invalid Guest Host response type")
    if command not in GUEST_HOST_REQUEST_COMMANDS:
        raise GuestHostProtocolError(f"unsupported Guest Host Request command: {command}")
    if not str(request_id).strip():
        raise GuestHostProtocolError("request_id must be a non-empty string")
    target = str(target_device_id).strip()
    if not target.isdigit():
        raise GuestHostProtocolError("target_device_id must contain a numeric device ID")
    return {
        "type": message_type,
        "event": message_type,
        "request_id": str(request_id).strip(),
        "command": command,
        "target_device_id": target,
        "on_behalf_of_device": target,
        "data": dict(data or {}),
    }
