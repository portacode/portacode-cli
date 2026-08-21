def dashboard_rollback_managed_device_snapshot(device_id, snapshot_name, progress_updates_for_user) -> str:
    from dashboard.services.codex_tool_gateway import manage_managed_device_snapshots
    from codex.tool_errors import ToolHandlerError
    try: return manage_managed_device_snapshots(request=request, device_id=device_id, action="rollback", snapshot_name=snapshot_name)
    except (PermissionError, ValueError, RuntimeError) as exc: raise ToolHandlerError(str(exc), payload={"error": str(exc)}) from exc
tool_definition={"name":"dashboard_rollback_managed_device_snapshot","description":"Rollback an authorized managed device to a named snapshot.","parameters":{"device_id":{"type":"integer"},"snapshot_name":{"type":"string"},"progress_updates_for_user":{"type":"string"}},"run":dashboard_rollback_managed_device_snapshot}
