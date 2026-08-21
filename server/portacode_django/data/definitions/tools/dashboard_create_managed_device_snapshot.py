def dashboard_create_managed_device_snapshot(device_id, snapshot_name, description, progress_updates_for_user) -> str:
    from dashboard.services.codex_tool_gateway import manage_managed_device_snapshots
    from codex.tool_errors import ToolHandlerError
    try: return manage_managed_device_snapshots(request=request, device_id=device_id, action="create", snapshot_name=snapshot_name, description=description)
    except (PermissionError, ValueError, RuntimeError) as exc: raise ToolHandlerError(str(exc), payload={"error": str(exc)}) from exc
tool_definition={"name":"dashboard_create_managed_device_snapshot","description":"Create a named snapshot of an authorized managed device.","parameters":{"device_id":{"type":"integer"},"snapshot_name":{"type":"string"},"description":{"type":"string"},"progress_updates_for_user":{"type":"string"}},"run":dashboard_create_managed_device_snapshot}
