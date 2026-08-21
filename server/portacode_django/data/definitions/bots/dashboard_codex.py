from codex.services.unicom_responses import unicom_responses_context
from dashboard.services.codex_context import system_instruction


bot_tools = [
    "dashboard_list_devices",
    "dashboard_read_device_file",
    "dashboard_view_device_image",
    "dashboard_generate_or_edit_image",
    "dashboard_browser_run",
    "dashboard_list_device_directory",
    "dashboard_get_device_path_info",
    "dashboard_search_device_files",
    "dashboard_write_device_file",
    "dashboard_manage_device_path",
    "dashboard_manage_device_project",
    "dashboard_list_chat_attachments",
    "dashboard_view_chat_attachment",
    "dashboard_copy_attachment_to_device",
    "dashboard_start_device_path_transfer",
    "dashboard_get_device_path_transfer",
    "dashboard_cancel_device_path_transfer",
    "dashboard_set_device_exposed_ports",
    "dashboard_control_managed_device_power",
    "dashboard_resize_managed_device",
    "dashboard_list_managed_device_snapshots",
    "dashboard_create_managed_device_snapshot",
    "dashboard_delete_managed_device_snapshots",
    "dashboard_rollback_managed_device_snapshot",
    "dashboard_connect_device_domain",
    "dashboard_disconnect_device_domain",
    "dashboard_manage_device_ingress",
    "dashboard_get_infrastructure",
    "dashboard_search_templates",
    "dashboard_get_template_yaml",
    "dashboard_search_docs",
    "dashboard_read_doc",
    "dashboard_terminal_exec",
    "dashboard_run_device_codex_task",
    "dashboard_start_device_automation",
    "dashboard_manage_device_automation",
    "dashboard_offer_device_upload",
    "dashboard_offer_device_download",
    "dashboard_provision_device",
    "dashboard_search_github_repositories",
    "dashboard_create_github_repository",
    "dashboard_list_github_directory",
    "dashboard_read_github_file",
    "dashboard_write_github_file",
    "dashboard_list_available_resources",
    "dashboard_request_permission",
    "dashboard_collect_user_input",
]


def handle_incoming_message(message, bot, tools_list):
    from dashboard.services.codex_context import filter_dashboard_codex_tools
    client, stream_sink = unicom_responses_context(request)
    model = (request.metadata or {}).get("model") or "gpt-5.6-sol"
    return bot.reply_using_llm(
        message,
        filter_dashboard_codex_tools(tools_list, request),
        request=request,
        api_mode="responses",
        openai_client=client,
        stream=True,
        stream_event_sink=stream_sink,
        model_default=model,
        system_instruction=system_instruction(request),
    )
