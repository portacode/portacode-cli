"""Strict host-side authorization gateway for Dashboard Codex device tools.

This module deliberately delegates transport to the existing data services. It does
not alter, bypass, or duplicate the WebSocket consumer/routing implementation.
"""

from __future__ import annotations

import json
import asyncio
import base64
import hashlib
import io
import mimetypes
import os
import posixpath
import uuid
import httpx
from django.conf import settings
from django.core.files import File
from django.db import models
from typing import Any
from data.models import Device
from dashboard.codex_config import ALL_CHAT_MARKERS, BOT_NAME, LEGACY_BOT_NAMES
from dashboard.models import DashboardCodexToolAudit
from dashboard.services.codex_permissions import (
    DashboardCodexPermissionDenied,
    device_access_level,
    require_device_access,
)
def manage_managed_device_snapshots(*, request, device_id: int, action: str, **kwargs) -> str:
    from dashboard.services.guest_host_requests import send_guest_host_request_and_wait, host_supports_guest_host_request
    from data.services.utils import sync_run_async
    user, device = authorize_device(request=request, device_id=device_id, tool_name=f"dashboard_{action}_managed_device_snapshot", write=action != "list", arguments=kwargs)
    if not device.proxmox_parent_id: raise ValueError("Snapshots apply only to managed devices.")
    parent = Device.objects.get(pk=device.proxmox_parent_id)
    if not host_supports_guest_host_request(parent, f"snapshots.{ 'read' if action == 'list' else action }"): raise RuntimeError("The Proxmox host must be updated before snapshot management is available.")
    command = {"list":"list_proxmox_container_snapshots","create":"create_proxmox_container_snapshot","delete":"delete_proxmox_container_snapshots","rollback":"rollback_proxmox_container_snapshot"}[action]
    payload={"child_device_id":str(device_id), **kwargs}
    _, envelope = sync_run_async(send_guest_host_request_and_wait(timeout=75, host_device_id=parent.pk, command=command, target_device_id=device_id, authorization={"principal_type":"user","principal_id":str(user.pk),"principal_role":"owner","principal_username":user.get_username(),"operation":f"snapshots.{ 'read' if action == 'list' else action }"}, payload=payload))
    data=envelope.get("data") or {}
    if not data.get("success"): raise RuntimeError(data.get("error") or "Snapshot operation failed.")
    data["_unicom_presentation"]={"type":"snapshot_operation","action":action,"device_id":device_id,"count":data.get("count"),"message":f"Snapshot {action} completed."}
    return json.dumps(data, default=str)


class DashboardCodexToolContextError(PermissionError):
    pass


def _request_user(request):
    """Derive identity from persisted request relationships, never tool arguments."""
    if request is None:
        raise DashboardCodexToolContextError("This tool requires a Dashboard Codex request.")
    initial = getattr(request, "initial_request", None) or request
    if (initial.metadata or {}).get("source") not in ALL_CHAT_MARKERS:
        raise DashboardCodexToolContextError("This tool requires a Dashboard Codex request.")
    from unibot.models import Bot
    if not Bot.objects.filter(
        name__in=(BOT_NAME, *LEGACY_BOT_NAMES), request_category_id=request.category_id
    ).exists():
        raise DashboardCodexToolContextError("The request was not assigned to the Dashboard Codex bot.")
    message = getattr(initial, "message", None)
    account = getattr(initial, "account", None)
    member = getattr(account, "member", None) if account else None
    user = getattr(member, "user", None)
    if not user or not message or message.user_id != user.pk:
        raise DashboardCodexToolContextError("Dashboard Codex request identity is inconsistent.")
    if (message.raw or {}).get("source") not in ALL_CHAT_MARKERS:
        raise DashboardCodexToolContextError("Dashboard Codex message scope is invalid.")
    return user


def _request_chat(request):
    initial = getattr(request, "initial_request", None) or request
    message = getattr(initial, "message", None)
    if not message or not message.chat_id:
        raise DashboardCodexToolContextError("Dashboard Codex request has no chat scope.")
    return message.chat


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep audit data bounded and JSON-safe; never log file contents or command output."""
    result = {}
    for key, value in arguments.items():
        if key in {"content", "content_base64", "diff", "password", "ssh_key"}:
            result[key] = "[redacted]"
            continue
        if value is None or isinstance(value, (bool, int, float)):
            result[key] = value
        else:
            result[key] = str(value)[:500]
    return result


def _authorized_project(user, device_id: int, project_id: str | None):
    if not project_id:
        return None
    import uuid as uuid_module
    from data.models import Project
    try:
        project_uuid = uuid_module.UUID(str(project_id))
    except (TypeError, ValueError) as exc:
        raise DashboardCodexPermissionDenied("Invalid project identifier.") from exc
    project = Project.objects.filter(
        uuid=project_uuid, user=user, device_id=device_id
    ).first()
    if not project:
        raise DashboardCodexPermissionDenied(
            "The project does not belong to the authorized device and user."
        )
    return project


def _absolute_path(value: str) -> str:
    path = str(value or "").strip()
    if not path.startswith("/") or "\x00" in path:
        raise ValueError("An absolute device path is required.")
    return posixpath.normpath(path)


def _run_device_command(*, user, device_id: int, project_id: str | None,
                        command: str, timeout: float = 30.0, **payload):
    """Send one documented control command through the authenticated gateway."""
    from data.services.device_client import DeviceServiceClient
    from data.services.utils import sync_run_async

    async def execute():
        client = DeviceServiceClient(user_id=user.pk, project_id=project_id or None)
        if not await client.connect():
            raise RuntimeError("Unable to connect to the authenticated device gateway.")
        try:
            return await client.send_device_command(
                int(device_id), command, timeout=timeout,
                require_request_id=True, **payload,
            )
        finally:
            await client.disconnect()

    result = sync_run_async(execute())
    if result is None:
        raise RuntimeError(f"The device did not answer {command} before the timeout.")
    if result.get("event") == "error" or result.get("success") is False:
        raise RuntimeError(str(result.get("error") or result.get("message") or f"{command} failed"))
    return result


def _audit(*, request, user, device, tool_name, access, decision, arguments, detail=""):
    DashboardCodexToolAudit.objects.create(
        request=request,
        user=user,
        device=device,
        tool_name=tool_name,
        required_access=access,
        decision=decision,
        arguments=_safe_arguments(arguments),
        detail=str(detail)[:255],
    )


def authorize_device(*, request, device_id: int, tool_name: str, write: bool, arguments=None):
    user = _request_user(request)
    access = "write" if write else "read"
    arguments = arguments or {"device_id": device_id}
    try:
        device = Device.objects.get(pk=device_id, user=user)
    except Device.DoesNotExist as exc:
        _audit(request=request, user=user, device=None, tool_name=tool_name, access=access,
               decision="denied", arguments=arguments, detail="Device is not owned by the request user.")
        raise DashboardCodexPermissionDenied("Dashboard Codex cannot access this device.") from exc
    try:
        require_device_access(user=user, device=device, write=write, chat=_request_chat(request))
    except DashboardCodexPermissionDenied as exc:
        _audit(request=request, user=user, device=device, tool_name=tool_name, access=access,
               decision="denied", arguments=arguments, detail=str(exc))
        raise
    _audit(request=request, user=user, device=device, tool_name=tool_name, access=access,
           decision="allowed", arguments=arguments)
    return user, device


def list_authorized_devices(*, request) -> str:
    user = _request_user(request)
    chat = _request_chat(request)
    from django.conf import settings
    public_origin = (getattr(settings, "DJANGO_PUBLIC_ORIGIN", "") or "").rstrip("/")
    rows = Device.objects.filter(user=user).prefetch_related("projects").order_by("name", "id")
    from dashboard.consumers.consumer_helpers import (
        active_device_connections,
        device_connections_lock,
    )
    with device_connections_lock:
        online_device_ids = set(active_device_connections)
    devices = [
        {
            "id": device.pk,
            "name": device.name,
            "last_seen": device.last_seen,
            "access_level": device_access_level(user=user, device=device, chat=chat),
            "managed": bool(device.proxmox_parent_id),
            "online": device.pk in online_device_ids,
            "parent_host_online": (
                device.proxmox_parent_id in online_device_ids
                if device.proxmox_parent_id
                else None
            ),
            "projects": [
                {
                    "uuid": str(project.uuid),
                    "name": project.name,
                    "absolute_path": project.folder_path,
                    "url": f"{public_origin}/project/{project.uuid}/",
                }
                for project in device.projects.all()
            ],
            "public_urls": [
                service for service in (device.exposed_services or [])
                if isinstance(service, dict) and service.get("url")
            ],
        }
        for device in rows
        if device_access_level(user=user, device=device, chat=chat) in ("read", "write")
    ]
    _audit(request=request, user=user, device=None, tool_name="dashboard_list_devices",
           access="read", decision="allowed", arguments={})
    return json.dumps({"devices": devices}, default=str)


def list_available_resources(*, request) -> str:
    """Metadata-only catalog so the model can identify a missing grant without reading contents."""
    user = _request_user(request)
    chat = _request_chat(request)
    from dashboard.models import DashboardCodexRepositoryPermission
    from data.models import GitHubRepository
    global_repos = dict(DashboardCodexRepositoryPermission.objects.filter(user=user, chat__isnull=True).values_list("repository_id", "access_level"))
    global_repos.update(DashboardCodexRepositoryPermission.objects.filter(user=user, chat=chat).values_list("repository_id", "access_level"))
    return json.dumps({
        "devices": [{"id": row.pk, "name": row.name, "access": device_access_level(user=user, device=row, chat=chat)} for row in Device.objects.filter(user=user).order_by("name")],
        "repositories": [{"id": row.pk, "name": row.full_name, "private": row.private, "access": global_repos.get(row.pk, "none")} for row in GitHubRepository.objects.filter(installation__installed_by=user, installation__disconnected_at__isnull=True, removed_at__isnull=True).order_by("full_name")],
    })


def request_permission(*, request, resource_type: str, resource_id: int, access_level: str, reason: str) -> str:
    user = _request_user(request)
    if resource_type == "device":
        resource = Device.objects.filter(pk=resource_id, user=user).values_list("name", flat=True).first()
    elif resource_type == "repository":
        resource = Device._meta.apps.get_model("data", "GitHubRepository").objects.filter(pk=resource_id, installation__installed_by=user, removed_at__isnull=True).values_list("full_name", flat=True).first()
    elif resource_type == "github_account":
        resource = Device._meta.apps.get_model("data", "GitHubInstallation").objects.filter(
            pk=resource_id, installed_by=user, disconnected_at__isnull=True,
            suspended_at__isnull=True,
        ).values_list("account_login", flat=True).first()
    else:
        raise ValueError("resource_type must be device, repository, or github_account")
    if not resource or access_level not in ("read", "write"):
        raise ValueError("Requested resource or access level is invalid")
    return json.dumps({"status": "awaiting_user_approval", "resource_type": resource_type, "resource_id": resource_id, "resource_name": resource, "access_level": access_level, "reason": str(reason)[:240]})


def collect_user_input(*, request, title: str, description: str, questions: list, submit_label: str) -> str:
    _request_user(request)
    if not isinstance(questions, list) or not 1 <= len(questions) <= 8:
        raise ValueError("questions must contain between 1 and 8 items")
    allowed_types = {"single_choice", "multi_choice", "text", "number", "attachment"}
    normalized, seen = [], set()
    for raw in questions:
        if not isinstance(raw, dict):
            raise ValueError("each question must be an object")
        question_id = str(raw.get("id") or "").strip()
        kind = str(raw.get("type") or "")
        label = str(raw.get("label") or "").strip()
        if not question_id or len(question_id) > 64 or not question_id.replace("_", "").replace("-", "").isalnum() or question_id in seen:
            raise ValueError("question ids must be unique letters, numbers, dashes, or underscores")
        if kind not in allowed_types or not label:
            raise ValueError("each question needs a valid type and label")
        seen.add(question_id)
        item = {"id": question_id, "type": kind, "label": label[:160], "help_text": str(raw.get("help_text") or "")[:240], "default": raw.get("default")}
        if kind in {"single_choice", "multi_choice"}:
            options = raw.get("options")
            if not isinstance(options, list) or not 2 <= len(options) <= 10:
                raise ValueError("choice questions need 2 to 10 options")
            item["options"] = [{"value": str(option.get("value", ""))[:100], "label": str(option.get("label", ""))[:120]} for option in options if isinstance(option, dict)]
            if len(item["options"]) != len(options) or any(not option["value"] or not option["label"] for option in item["options"]):
                raise ValueError("choice options need non-empty values and labels")
        if kind == "number":
            item.update({key: raw.get(key) for key in ("min", "max", "step")})
        if kind == "attachment":
            item.update({"accept": str(raw.get("accept") or "")[:200], "multiple": bool(raw.get("multiple"))})
        normalized.append(item)
    return json.dumps({"status": "awaiting_user_input", "title": str(title)[:120], "description": str(description)[:500], "submit_label": str(submit_label or "Continue")[:40], "questions": normalized})


def search_github_repositories(*, request, query: str, visibility: str = "granted", language: str = "", sort: str = "best-match", page: int = 1, per_page: int = 10) -> str:
    """Search compact granted-repository metadata or GitHub's public repository index."""
    user = _request_user(request)
    chat = _request_chat(request)
    query = str(query or "").strip()
    page, per_page = max(int(page), 1), min(max(int(per_page), 1), 30)
    if visibility == "granted":
        from dashboard.models import DashboardCodexRepositoryPermission
        global_levels = dict(DashboardCodexRepositoryPermission.objects.filter(user=user, chat__isnull=True).values_list("repository_id", "access_level"))
        global_levels.update(DashboardCodexRepositoryPermission.objects.filter(user=user, chat=chat).values_list("repository_id", "access_level"))
        allowed = [key for key, value in global_levels.items() if value in ("read", "write")]
        rows = Device._meta.apps.get_model("data", "GitHubRepository").objects.filter(pk__in=allowed)
        if query:
            rows = rows.filter(full_name__icontains=query)
        total = rows.count()
        rows = rows.order_by("full_name")[(page - 1) * per_page:page * per_page]
        return json.dumps({"source": "granted", "total": total, "page": page, "repositories": [
            {"id": row.pk, "full_name": row.full_name, "private": row.private, "access": global_levels[row.pk], "default_branch": row.default_branch}
            for row in rows
        ]})
    qualifiers = [query or "stars:>=1", "is:public"]
    if language:
        qualifiers.append(f"language:{str(language)[:50]}")
    response = httpx.get("https://api.github.com/search/repositories", params={
        "q": " ".join(qualifiers), "sort": "" if sort == "best-match" else sort,
        "order": "desc", "page": page, "per_page": per_page,
    }, headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}, timeout=15.0)
    response.raise_for_status()
    payload = response.json()
    return json.dumps({"source": "github-public", "total": payload.get("total_count", 0), "page": page, "repositories": [
        {"full_name": item.get("full_name"), "description": item.get("description"), "language": item.get("language"), "stars": item.get("stargazers_count"), "updated_at": item.get("updated_at"), "url": item.get("html_url")}
        for item in payload.get("items", [])
    ]})


def create_github_repository(*, request, connection_id: int, name: str,
                             private: bool = True, description: str = "",
                             source_device_id: int | None = None) -> str:
    """Create a repository under one permitted account and grant this chat write access."""
    user = _request_user(request)
    chat = _request_chat(request)
    from django.db import transaction
    from dashboard.models import (
        DashboardCodexGitHubAccountPermission, DashboardCodexRepositoryPermission,
    )
    from data.models import GitHubInstallation
    from data.services.github_app import create_installation_repository, set_device_access

    try:
        installation = GitHubInstallation.objects.get(
            models.Q(pk=int(connection_id)) | models.Q(installation_id=int(connection_id)),
            installed_by=user, disconnected_at__isnull=True, suspended_at__isnull=True,
        )
    except (GitHubInstallation.DoesNotExist, ValueError, TypeError) as exc:
        raise DashboardCodexPermissionDenied("GitHub account is unavailable.") from exc
    global_allowed = DashboardCodexGitHubAccountPermission.objects.filter(
        user=user, chat__isnull=True, installation=installation,
    ).values_list("can_create_repositories", flat=True).first()
    global_allowed = True if global_allowed is None else global_allowed
    thread_allowed = DashboardCodexGitHubAccountPermission.objects.filter(
        user=user, chat=chat, installation=installation,
    ).values_list("can_create_repositories", flat=True).first()
    allowed = global_allowed if thread_allowed is None else thread_allowed
    if not allowed:
        raise DashboardCodexPermissionDenied(
            "This chat is not allowed to create repositories in that GitHub account."
        )
    source_device = None
    if source_device_id is not None:
        try:
            source_device = Device.objects.get(pk=int(source_device_id), user=user)
        except Device.DoesNotExist as exc:
            raise DashboardCodexPermissionDenied("Source device is unavailable.") from exc
        require_device_access(user=user, device=source_device, chat=chat, write=True)
    with transaction.atomic():
        repository = create_installation_repository(
            installation=installation, name=name, private=private,
            description=description,
        )
        DashboardCodexRepositoryPermission.objects.update_or_create(
            user=user, chat=chat, repository=repository,
            defaults={"access_level": "write"},
        )
        if source_device is not None:
            set_device_access(
                user=user, device=source_device, repository=repository,
                access_level="write",
            )
    _audit(
        request=request, user=user, device=None,
        tool_name="dashboard_create_github_repository", access="write",
        decision="allowed", arguments={
            "installation_id": installation.pk, "name": repository.name,
            "private": repository.private,
            "source_device_id": source_device.pk if source_device else None,
        },
    )
    return json.dumps({
        "repository_id": repository.pk, "full_name": repository.full_name,
        "private": repository.private,
        "clone_url": f"https://github.com/{repository.full_name}.git",
    })


def _authorized_github_repository(*, request, repository_id: int, write: bool = False):
    """Return one repository after rechecking the request's chat-local grant."""
    user = _request_user(request)
    chat = _request_chat(request)
    from dashboard.models import DashboardCodexRepositoryPermission
    from data.models import GitHubRepository
    global_level = DashboardCodexRepositoryPermission.objects.filter(
        user=user, chat__isnull=True, repository_id=repository_id
    ).values_list("access_level", flat=True).first() or "none"
    level = DashboardCodexRepositoryPermission.objects.filter(
        user=user, chat=chat, repository_id=repository_id
    ).values_list("access_level", flat=True).first()
    level = global_level if level is None else level
    if level not in (("write",) if write else ("read", "write")):
        action = "write to" if write else "read"
        raise DashboardCodexPermissionDenied(f"This chat is not allowed to {action} that repository.")
    try:
        repository = GitHubRepository.objects.select_related("installation").get(
            pk=repository_id, installation__installed_by=user,
            installation__disconnected_at__isnull=True, removed_at__isnull=True,
        )
    except GitHubRepository.DoesNotExist as exc:
        raise DashboardCodexPermissionDenied("Repository is unavailable.") from exc
    return repository


def _safe_github_path(path: str, *, allow_root: bool = False) -> str:
    clean_path = str(path or "").strip().lstrip("/")
    if clean_path in {"", "."} and allow_root:
        return ""
    if not clean_path or ".." in clean_path.split("/"):
        raise ValueError("A safe repository-relative file path is required.")
    return clean_path


def _github_not_found(exc: Exception) -> bool:
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


def list_github_directory(*, request, repository_id: int, path: str = "", ref: str = "",
                          offset: int = 0, limit: int = 100) -> str:
    """List one repository directory so callers can discover paths without guessing."""
    repository = _authorized_github_repository(request=request, repository_id=repository_id)
    clean_path = _safe_github_path(path, allow_root=True)
    from data.services.github_app import installation_github_client
    from githubkit.exception import RequestFailed

    client = installation_github_client(
        repository.installation, repository_id=repository.github_repository_id
    )
    kwargs = {"ref": str(ref).strip()} if str(ref).strip() else {}
    try:
        response = client.rest.repos.get_content(
            repository.owner_login, repository.name, clean_path, **kwargs
        )
    except RequestFailed as exc:
        if _github_not_found(exc):
            return json.dumps({
                "repository": repository.full_name, "path": clean_path,
                "ref": ref or repository.default_branch, "exists": False,
                "error": "not_found", "entries": [],
            })
        raise
    payload = response.raw_response.json()
    if not isinstance(payload, list):
        raise ValueError("The requested repository path is a file, not a directory.")
    offset = max(int(offset), 0)
    limit = min(max(int(limit), 1), 100)
    page = payload[offset:offset + limit]
    entries = [{
        "name": str(item.get("name") or "")[:255],
        "path": str(item.get("path") or "")[:1000],
        "type": str(item.get("type") or "")[:32],
        "size": item.get("size"),
    } for item in page if isinstance(item, dict)]
    return json.dumps({
        "repository": repository.full_name, "path": clean_path,
        "ref": ref or repository.default_branch, "exists": True,
        "entries": entries, "offset": offset, "limit": limit,
        "total_entries": len(payload),
        "next_offset": offset + len(page) if offset + len(page) < len(payload) else None,
    })


def read_github_file(*, request, repository_id: int, path: str, ref: str = "",
                     offset: int = 0, max_chars: int = 12000) -> str:
    """Read one bounded text file after rechecking the chat-local repository grant."""
    repository = _authorized_github_repository(request=request, repository_id=repository_id)
    clean_path = _safe_github_path(path)
    from data.services.github_app import installation_github_client
    from githubkit.exception import RequestFailed
    client = installation_github_client(repository.installation, repository_id=repository.github_repository_id)
    kwargs = {"ref": str(ref).strip()} if str(ref).strip() else {}
    try:
        response = client.rest.repos.get_content(
            repository.owner_login, repository.name, clean_path,
            **kwargs, headers={"Accept": "application/vnd.github.raw+json"},
        )
    except RequestFailed as exc:
        if _github_not_found(exc):
            return json.dumps({
                "repository": repository.full_name, "path": clean_path,
                "ref": ref or repository.default_branch, "exists": False,
                "error": "not_found",
            })
        raise
    content = response.content
    blob_sha = hashlib.sha1(
        f"blob {len(content)}\0".encode("ascii") + content
    ).hexdigest()
    if len(content) > 512 * 1024:
        raise ValueError("Repository files larger than 512 KiB are not returned to chat.")
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        decoded = None
    if decoded is None or "\x00" in decoded:
        content_type = str(response.raw_response.headers.get("content-type") or "").split(";", 1)[0]
        return json.dumps({
            "repository": repository.full_name, "path": clean_path,
            "ref": ref or repository.default_branch, "exists": True,
            "binary": True, "size": len(content), "content_type": content_type,
            "sha": blob_sha,
            "message": "Binary repository files are not returned as text.",
        })
    offset = max(int(offset), 0)
    max_chars = min(max(int(max_chars), 1), 12000)
    excerpt = decoded[offset:offset + max_chars]
    end_offset = offset + len(excerpt)
    return json.dumps({
        "repository": repository.full_name, "path": clean_path,
        "ref": ref or repository.default_branch, "exists": True,
        "binary": False, "content": excerpt, "offset": offset,
        "sha": blob_sha,
        "end_offset": end_offset, "total_chars": len(decoded),
        "next_offset": end_offset if end_offset < len(decoded) else None,
        "truncated": end_offset < len(decoded),
    })


def write_github_file(*, request, repository_id: int, path: str, content: str,
                      commit_message: str, branch: str = "",
                      expected_sha: str = "") -> str:
    """Create or replace one UTF-8 file with optimistic concurrency protection."""
    repository = _authorized_github_repository(
        request=request, repository_id=repository_id, write=True,
    )
    clean_path = _safe_github_path(path)
    encoded_content = str(content).encode("utf-8")
    if len(encoded_content) > 512 * 1024:
        raise ValueError("Repository file content cannot exceed 512 KiB.")
    message = str(commit_message or "").strip()
    if not message or len(message) > 250:
        raise ValueError("A commit message between 1 and 250 characters is required.")
    clean_sha = str(expected_sha or "").strip()
    if clean_sha and (
        len(clean_sha) != 40 or any(char not in "0123456789abcdefABCDEF" for char in clean_sha)
    ):
        raise ValueError("expected_sha must be an empty string or a 40-character Git blob SHA.")
    from data.services.github_app import installation_github_client
    client = installation_github_client(
        repository.installation, repository_id=repository.github_repository_id,
        contents_permission="write",
    )
    data = {
        "message": message,
        "content": base64.b64encode(encoded_content).decode("ascii"),
    }
    clean_branch = str(branch or "").strip()
    if clean_branch:
        data["branch"] = clean_branch
    if clean_sha:
        data["sha"] = clean_sha
    response = client.rest.repos.create_or_update_file_contents(
        repository.owner_login, repository.name, clean_path, data=data,
    )
    remote = response.parsed_data
    commit = getattr(remote, "commit", None)
    content_result = getattr(remote, "content", None)
    _audit(
        request=request, user=_request_user(request), device=None,
        tool_name="dashboard_write_github_file", access="write",
        decision="allowed", arguments={
            "repository_id": repository.pk, "path": clean_path,
            "branch": clean_branch or repository.default_branch,
        },
    )
    return json.dumps({
        "repository": repository.full_name,
        "path": clean_path,
        "commit_sha": str(getattr(commit, "sha", "") or ""),
        "content_sha": str(getattr(content_result, "sha", "") or ""),
        "branch": clean_branch or repository.default_branch,
    })


def read_device_file(*, request, device_id: int, absolute_path: str,
                     start_line: int, max_lines: int) -> str:
    arguments = {"device_id": device_id, "absolute_path": absolute_path,
                 "start_line": start_line, "max_lines": max_lines}
    user, _ = authorize_device(request=request, device_id=device_id,
                               tool_name="dashboard_read_device_file", write=False,
                               arguments=arguments)
    from data.services.device_service import DeviceService
    from data.services.utils import sync_run_async
    service = DeviceService(device_id, user.pk)
    result = sync_run_async(service.read_file(
        str(absolute_path), start_line=max(int(start_line), 1),
        max_lines=min(max(int(max_lines), 1), 1000),
    ))
    return json.dumps(result, default=str)


def control_managed_device_power(*, request, device_id: int, action: str) -> str:
    action = str(action or "").strip().lower()
    if action not in {"start", "stop", "reboot"}:
        raise ValueError("action must be start, stop, or reboot")
    user, device = authorize_device(
        request=request,
        device_id=device_id,
        tool_name="dashboard_control_managed_device_power",
        write=True,
        arguments={"device_id": device_id, "action": action},
    )
    if not device.proxmox_parent_id:
        raise ValueError("Power controls apply only to managed devices.")
    token = getattr(settings, "UNICOM_STREAM_RELAY_TOKEN", "") or ""
    if not token:
        raise RuntimeError("The internal action relay is not configured.")
    url = getattr(settings, "UNICOM_ACTION_RELAY_INTERNAL_URL", "") or "http://django:8001/internal/unicom/action/"
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "request_id": str(request.pk),
                "user_id": user.pk,
                "action": "managed_device_power",
                "arguments": {"device_id": device_id, "action": action},
            },
            timeout=75.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError("The managed-device power service could not be reached.") from exc
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("The managed-device power service returned an invalid response.") from exc
    if not response.is_success:
        raise RuntimeError(str(result.get("error") or "The power operation failed."))
    proxmox_response = result.get("response") or {}
    result["_unicom_presentation"] = {
        "type": "power_operation",
        "action": action,
        "device_id": device_id,
        "device_name": device.name,
        "success": True,
        "status": proxmox_response.get("status"),
        "message": proxmox_response.get("message") or f"{action.title()} completed.",
    }
    return json.dumps(result, default=str)


def resize_managed_device_resources(*, request, device_id: int, cpus: float,
                                    ram_mib: int, disk_gib: int) -> str:
    user, device = authorize_device(
        request=request,
        device_id=device_id,
        tool_name="dashboard_resize_managed_device",
        write=True,
        arguments={
            "device_id": device_id,
            "cpus": cpus,
            "ram_mib": ram_mib,
            "disk_gib": disk_gib,
        },
    )
    if not device.proxmox_parent_id:
        raise ValueError("Resource resizing applies only to managed devices.")
    token = getattr(settings, "UNICOM_STREAM_RELAY_TOKEN", "") or ""
    if not token:
        raise RuntimeError("The internal action relay is not configured.")
    url = getattr(settings, "UNICOM_ACTION_RELAY_INTERNAL_URL", "") or "http://django:8001/internal/unicom/action/"
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "request_id": str(request.pk),
                "user_id": user.pk,
                "action": "managed_device_resize",
                "arguments": {
                    "device_id": device_id,
                    "cpus": cpus,
                    "ram_mib": ram_mib,
                    "disk_gib": disk_gib,
                },
            },
            timeout=90.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError("The managed-device resize service could not be reached.") from exc
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("The managed-device resize service returned an invalid response.") from exc
    if not response.is_success:
        raise RuntimeError(str(result.get("error") or "The resize operation failed."))
    result["_unicom_presentation"] = {
        "type": "resource_resize",
        "device_id": device_id,
        "device_name": result.get("device_name") or device.name,
        "success": bool(result.get("success")),
        "status": result.get("status"),
        "before": result.get("before") or {},
        "after": result.get("after") or {},
        "message": result.get("message") or "Resources updated.",
    }
    return json.dumps(result, default=str)


def view_device_image(*, request, device_id: int, absolute_path: str) -> str:
    """Place a small authorized device image directly in Responses multimodal context."""
    absolute_path = _absolute_path(absolute_path)
    arguments = {"device_id": device_id, "absolute_path": absolute_path}
    user, _ = authorize_device(
        request=request,
        device_id=device_id,
        tool_name="dashboard_view_device_image",
        write=False,
        arguments=arguments,
    )
    from data.services.device_service import DeviceService
    from data.services.utils import sync_run_async

    result = sync_run_async(DeviceService(device_id, user.pk).read_image(absolute_path))
    if not result or result.get("error"):
        raise ValueError((result or {}).get("error") or "The image could not be read from the device.")
    mime_type = str(result.get("mime_type") or "")
    encoded = str(result.get("content_base64") or "")
    if not mime_type.startswith("image/") or not encoded:
        raise ValueError("The device returned an invalid image response.")
    image_url = f"data:{mime_type};base64,{encoded}"
    return json.dumps({
        "path": absolute_path,
        "mime_type": mime_type,
        "size": result.get("size"),
        "_unicom_presentation": {
            "type": "image", "url": image_url,
            "alt": f"Image from device path {absolute_path}",
            "caption": absolute_path,
        },
        "_responses_content": [
            {"type": "input_text", "text": f"Image loaded from device path {absolute_path}."},
            {"type": "input_image", "image_url": image_url, "detail": "high"},
        ],
    })


def browser_run(*, request, device_id: int, session_id: str, start_url: str | None,
                steps: list, timeout_ms: int, final_screenshot: bool,
                screenshot_on_failure: bool, record_video: bool,
                viewport: dict | None = None) -> str:
    """Run a bounded action batch in a persistent device-side browser profile."""
    arguments = {
        "device_id": device_id, "session_id": session_id, "start_url": start_url,
        "step_count": len(steps) if isinstance(steps, list) else None,
        "record_video": record_video,
    }
    user, device = authorize_device(
        request=request, device_id=device_id, tool_name="dashboard_browser_run",
        write=True, arguments=arguments,
    )
    if not isinstance(steps, list) or len(steps) > 30:
        raise ValueError("steps must contain at most 30 actions")
    allowed = {"goto", "click", "fill", "press", "select", "check", "uncheck", "wait", "assert", "read", "screenshot"}
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or step.get("op") not in allowed:
            raise ValueError(f"Invalid browser operation at step {index}.")
    result = _run_device_command(
        user=user, device_id=device_id, project_id=None, command="browser_run",
        timeout=min(300, 30 + len(steps) * 30), session_id=session_id or "default",
        start_url=start_url, steps=steps, timeout_ms=min(max(int(timeout_ms), 250), 30_000),
        final_screenshot=bool(final_screenshot), screenshot_on_failure=bool(screenshot_on_failure),
        record_video=bool(record_video), viewport=viewport or {"width": 960, "height": 540},
    )
    screenshots = result.pop("screenshots", [])
    blocks = [{"type": "input_text", "text": f"Browser run finished at {result.get('url') or 'an unknown URL'}."}]
    images = []
    total = 0
    for shot in screenshots[:5]:
        encoded = str(shot.get("data") or "")
        total += len(encoded)
        if not encoded or total > 6_000_000:
            break
        url = f"data:{shot.get('mime_type') or 'image/jpeg'};base64,{encoded}"
        item = {"url": url, "alt": f"Browser screenshot after step {shot.get('step')}", "caption": f"Step {shot.get('step')}"}
        images.append(item)
        blocks.append({"type": "input_image", "image_url": url, "detail": "high"})
    presentation = None
    if result.get("video_path"):
        presentation = {
            "type": "video", "device_id": device_id, "source_path": result["video_path"],
            "caption": f"Browser recording for session {session_id}",
            "poster": images[-1]["url"] if images else "",
        }
    elif len(images) > 1:
        presentation = {"type": "gallery", "images": images}
    elif images:
        presentation = {"type": "image", **images[0]}
    if presentation:
        result["_unicom_presentation"] = presentation
    result["_responses_content"] = blocks
    return json.dumps(result, default=str)


def list_device_directory(*, request, device_id: int, absolute_path: str,
                          show_hidden: bool, limit: int, offset: int) -> str:
    arguments = {"device_id": device_id, "absolute_path": absolute_path,
                 "show_hidden": show_hidden, "limit": limit, "offset": offset}
    user, _ = authorize_device(request=request, device_id=device_id,
                               tool_name="dashboard_list_device_directory", write=False,
                               arguments=arguments)
    from data.services.device_service import DeviceService
    from data.services.utils import sync_run_async
    service = DeviceService(device_id, user.pk)
    result = sync_run_async(service.list_directory(
        str(absolute_path), show_hidden=bool(show_hidden),
        limit=min(max(int(limit), 1), 500), offset=max(int(offset), 0),
    ))
    return json.dumps(result, default=str)


def _chat_attachment_ids(chat, user) -> list[str]:
    attachment_ids = []
    for raw in chat.messages.filter(user=user).values_list("raw", flat=True):
        if not isinstance(raw, dict):
            continue
        direct = raw.get("dashboard_attachment_id")
        if direct:
            attachment_ids.append(str(direct))
    for raw in chat.messages.filter(media_type="tool_response").values_list("raw", flat=True):
        if not isinstance(raw, dict):
            continue
        response = raw.get("tool_response") or {}
        if response.get("tool_name") != "dashboard_collect_user_input":
            continue
        answers = (response.get("result") or {}).get("answers") or {}
        for value in answers.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and item.get("id"):
                    attachment_ids.append(str(item["id"]))
    return list(dict.fromkeys(attachment_ids))


def list_chat_attachments(*, request) -> str:
    """Return deployable attachment metadata for this chat, never storage paths."""
    user = _request_user(request)
    chat = _request_chat(request)
    attachment_ids = _chat_attachment_ids(chat, user)
    from dashboard.models import DashboardCodexAttachment
    rows = DashboardCodexAttachment.objects.filter(user=user, pk__in=attachment_ids)
    by_id = {str(row.pk): row for row in rows}
    attachments = []
    for attachment_id in dict.fromkeys(str(value) for value in attachment_ids):
        row = by_id.get(attachment_id)
        if row:
            attachments.append({
                "id": attachment_id,
                "name": row.name,
                "content_type": row.content_type,
                "size": row.size,
                "created_at": row.created_at,
            })
    for message in chat.messages.filter(is_outgoing=True).exclude(media=""):
        name = os.path.basename(message.media.name)
        attachments.append({
            "id": f"message:{message.pk}",
            "name": name,
            "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "size": message.media.size,
            "created_at": message.timestamp,
            "source": "generated",
        })
    _audit(request=request, user=user, device=None,
           tool_name="dashboard_list_chat_attachments", access="read",
           decision="allowed", arguments={})
    return json.dumps({"attachments": attachments}, default=str)


def view_chat_attachment(*, request, attachment_id: str) -> str:
    """Explicitly load a compatible chat attachment into model context.

    Questionnaire responses remain metadata-only. Bytes are read only when the
    model calls this tool for one attachment in the exact authenticated chat.
    """
    user = _request_user(request)
    chat = _request_chat(request)
    attachment_id = str(attachment_id or "").strip()
    arguments = {"attachment_id": attachment_id}
    if not attachment_id:
        raise DashboardCodexPermissionDenied("Attachment is unavailable.")
    if attachment_id.startswith("message:"):
        message_id = attachment_id.removeprefix("message:")
        try:
            message = chat.messages.get(
                pk=message_id, is_outgoing=True, media__isnull=False,
            )
        except chat.messages.model.DoesNotExist as exc:
            raise DashboardCodexPermissionDenied("Attachment is unavailable.") from exc
        attachment_file = message.media
        if not attachment_file.name:
            raise DashboardCodexPermissionDenied("Attachment is unavailable.")
        attachment_name = os.path.basename(attachment_file.name)
        attachment_type = (
            mimetypes.guess_type(attachment_name)[0] or "application/octet-stream"
        )
        attachment_size = attachment_file.size
        attachment_label = "Generated chat attachment"
    else:
        if attachment_id not in _chat_attachment_ids(chat, user):
            _audit(request=request, user=user, device=None,
                   tool_name="dashboard_view_chat_attachment", access="read",
                   decision="denied", arguments=arguments,
                   detail="Attachment is not linked to the authenticated chat.")
            raise DashboardCodexPermissionDenied("That attachment is not part of this chat.")
        from dashboard.models import DashboardCodexAttachment
        try:
            attachment = DashboardCodexAttachment.objects.get(pk=attachment_id, user=user)
        except (DashboardCodexAttachment.DoesNotExist, ValueError) as exc:
            raise DashboardCodexPermissionDenied("Attachment is unavailable.") from exc
        attachment_file = attachment.file
        attachment_name = attachment.name
        attachment_type = attachment.content_type or "application/octet-stream"
        attachment_size = attachment.size
        attachment_label = "User attachment"

    metadata = {
        "id": attachment_id, "name": attachment_name,
        "content_type": attachment_type, "size": attachment_size,
    }
    mime_type = metadata["content_type"].split(";", 1)[0].strip().lower()
    image_types = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    text_types = {
        "application/json", "application/ld+json", "application/xml",
        "application/x-yaml", "application/yaml", "application/javascript",
        "application/sql", "image/svg+xml",
    }
    text_extensions = {
        ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".xml",
        ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".log",
        ".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".htm",
        ".sh", ".sql", ".java", ".c", ".h", ".cpp", ".hpp", ".rs",
        ".go", ".rb", ".php", ".swift", ".kt", ".svg",
    }
    document_extensions = {
        ".pdf", ".doc", ".docx", ".odt", ".rtf",
        ".ppt", ".pptx", ".odp", ".xls", ".xlsx", ".ods",
    }
    suffix = os.path.splitext(attachment_name.lower())[1]
    result = dict(metadata)
    with attachment_file.open("rb") as uploaded:
        if mime_type in image_types:
            if attachment_size > 5 * 1024 * 1024:
                result.update({"loaded": False, "reason": "Images larger than 5 MiB are not loaded into model context."})
            else:
                encoded = base64.b64encode(uploaded.read()).decode("ascii")
                image_url = f"data:{mime_type};base64,{encoded}"
                result.update({
                    "loaded": True,
                    "_unicom_presentation": {"type": "image", "url": image_url,
                                               "alt": attachment_name, "caption": attachment_name},
                    "_responses_content": [
                        {"type": "input_text", "text": f"{attachment_label} loaded: {attachment_name}."},
                        {"type": "input_image", "image_url": image_url, "detail": "high"},
                    ],
                })
        elif mime_type.startswith("text/") or mime_type in text_types or suffix in text_extensions:
            limit = 512 * 1024
            payload = uploaded.read(limit + 1)
            truncated = len(payload) > limit
            text_content = payload[:limit].decode("utf-8", errors="replace")
            result.update({
                "loaded": True, "truncated": truncated,
                "_responses_content": [{
                    "type": "input_text",
                    "text": f"{attachment_label} {attachment_name}{' (truncated to 512 KiB)' if truncated else ''}:\n\n{text_content}",
                }],
            })
        elif suffix in document_extensions:
            if attachment_size > 5 * 1024 * 1024:
                result.update({"loaded": False, "reason": "Documents larger than 5 MiB are not loaded into model context."})
            else:
                encoded = base64.b64encode(uploaded.read()).decode("ascii")
                result.update({
                    "loaded": True,
                    "_responses_content": [{
                        "type": "input_file", "filename": attachment_name,
                        "file_data": f"data:{mime_type};base64,{encoded}",
                    }],
                })
        else:
            result.update({
                "loaded": False,
                "reason": "This binary format cannot be loaded directly into model context. It can still be copied to an authorized device.",
            })
    _audit(request=request, user=user, device=None,
           tool_name="dashboard_view_chat_attachment", access="read",
           decision="allowed", arguments=arguments,
           detail="Loaded compatible content." if result["loaded"] else result["reason"])
    return json.dumps(result)


def _image_attachment_for_edit(*, request, attachment_id: str) -> tuple[str, str, bytes]:
    """Resolve one chat-scoped raster attachment without exposing storage paths."""
    user = _request_user(request)
    chat = _request_chat(request)
    attachment_id = str(attachment_id or "").strip()
    if attachment_id.startswith("message:"):
        try:
            message = chat.messages.get(
                pk=attachment_id.removeprefix("message:"),
                is_outgoing=True,
                media__isnull=False,
            )
        except chat.messages.model.DoesNotExist as exc:
            raise DashboardCodexPermissionDenied("Attachment is unavailable.") from exc
        image_file = message.media
        name = os.path.basename(image_file.name)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    else:
        if attachment_id not in _chat_attachment_ids(chat, user):
            raise DashboardCodexPermissionDenied("That attachment is not part of this chat.")
        from dashboard.models import DashboardCodexAttachment

        try:
            attachment = DashboardCodexAttachment.objects.get(pk=attachment_id, user=user)
        except (DashboardCodexAttachment.DoesNotExist, ValueError) as exc:
            raise DashboardCodexPermissionDenied("Attachment is unavailable.") from exc
        image_file = attachment.file
        name = attachment.name
        content_type = attachment.content_type or "application/octet-stream"
    content_type = content_type.split(";", 1)[0].strip().lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("Image editing supports JPEG, PNG, and WebP attachments.")
    if image_file.size > 50 * 1024 * 1024:
        raise ValueError("Each image attachment must be 50 MiB or smaller.")
    with image_file.open("rb") as opened:
        content = opened.read()
    if not content:
        raise ValueError("Image attachments cannot be empty.")
    return name, content_type, content


def generate_or_edit_image(
    *, request, prompt: str, attachment_ids: list[str] | None = None,
    size: str = "1024x1024", quality: str = "auto",
) -> str:
    """Use the metered Images API for all Dashboard generation and editing."""
    user = _request_user(request)
    chat = _request_chat(request)
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    attachment_ids = [str(value) for value in (attachment_ids or [])]
    if len(attachment_ids) > 16:
        raise ValueError("At most 16 source images are supported.")

    from codex.gateway_client import responses_client_for_user

    client = responses_client_for_user(
        user.pk,
        usage_nonce=f"dashboard-image-{request.pk}-{uuid.uuid4()}",
        dashboard_chat_id=str(chat.pk),
        dashboard_request_id=str(request.pk),
    )
    try:
        common = {
            "model": "gpt-image-2", "prompt": prompt,
            "size": size or "1024x1024", "quality": quality or "auto",
            "output_format": "png",
        }
        if attachment_ids:
            uploads = []
            for attachment_id in attachment_ids:
                name, _content_type, content = _image_attachment_for_edit(
                    request=request, attachment_id=attachment_id
                )
                upload = io.BytesIO(content)
                upload.name = name
                uploads.append(upload)
            response = client.images.edit(image=uploads, **common)
            operation = "edit"
        else:
            response = client.images.generate(**common)
            operation = "generation"
    finally:
        client.close()

    data = getattr(response, "data", None) or []
    encoded = getattr(data[0], "b64_json", None) if data else None
    if not encoded:
        raise RuntimeError("The image service returned no image data.")
    image_url = f"data:image/png;base64,{encoded}"
    _audit(
        request=request, user=user, device=None,
        tool_name="dashboard_generate_or_edit_image", access="write",
        decision="allowed",
        arguments={"operation": operation, "attachment_ids": attachment_ids,
                   "size": size, "quality": quality},
        detail=f"Metered image {operation} completed.",
    )
    return json.dumps({
        "operation": operation,
        "model": "gpt-image-2",
        "size": size,
        "_unicom_presentation": {
            "type": "image", "url": image_url,
            "alt": "Generated image", "caption": prompt[:500],
        },
        "_responses_content": [
            {"type": "input_text", "text": f"Metered image {operation} completed."},
            {"type": "input_image", "image_url": image_url, "detail": "high"},
        ],
    })


def get_device_path_info(*, request, device_id: int, absolute_path: str) -> str:
    absolute_path = _absolute_path(absolute_path)
    arguments = {"device_id": device_id, "absolute_path": absolute_path}
    user, _ = authorize_device(
        request=request, device_id=device_id,
        tool_name="dashboard_get_device_path_info", write=False,
        arguments=arguments,
    )
    result = _run_device_command(
        user=user, device_id=device_id, project_id=None,
        command="file_info", path=str(absolute_path),
    )
    return json.dumps(result, default=str)


def search_device_files(*, request, device_id: int, root_path: str, query: str,
                        match_case: bool, regex: bool, include_hidden: bool,
                        max_results: int) -> str:
    root_path = _absolute_path(root_path)
    arguments = {
        "device_id": device_id, "root_path": root_path, "query": query,
        "match_case": match_case, "regex": regex,
        "include_hidden": include_hidden, "max_results": max_results,
    }
    user, _ = authorize_device(
        request=request, device_id=device_id,
        tool_name="dashboard_search_device_files", write=False,
        arguments=arguments,
    )
    if not str(query or ""):
        raise ValueError("query is required")
    result = _run_device_command(
        user=user, device_id=device_id, project_id=None,
        command="file_search", timeout=30,
        root_path=str(root_path), query=str(query),
        match_case=bool(match_case), regex=bool(regex),
        whole_word=False, include_hidden=bool(include_hidden),
        max_results=min(max(int(max_results), 1), 100),
        max_matches_per_file=5, max_file_size=1024 * 1024,
        max_line_length=300,
    )
    return json.dumps(result, default=str)


def write_device_file(*, request, device_id: int, project_id: str | None,
                      absolute_path: str, content: str) -> str:
    absolute_path = _absolute_path(absolute_path)
    content = str(content)
    if len(content.encode("utf-8")) > 256 * 1024:
        raise ValueError("Text writes are limited to 256 KiB; use an attachment for larger files.")
    arguments = {
        "device_id": device_id, "project_id": project_id,
        "absolute_path": absolute_path, "content": content,
    }
    user, _ = authorize_device(
        request=request, device_id=device_id,
        tool_name="dashboard_write_device_file", write=True,
        arguments=arguments,
    )
    _authorized_project(user, device_id, project_id)
    result = _run_device_command(
        user=user, device_id=device_id, project_id=project_id,
        command="file_write", path=str(absolute_path), content=content,
    )
    return json.dumps(result, default=str)


def manage_device_path(*, request, device_id: int, project_id: str | None,
                       operation: str, absolute_path: str,
                       new_name: str = "", recursive: bool = False) -> str:
    operation = str(operation or "").strip()
    absolute_path = _absolute_path(absolute_path)
    command_by_operation = {
        "create_folder": "folder_create",
        "rename": "file_rename",
        "delete": "file_delete",
    }
    command = command_by_operation.get(operation)
    if not command:
        raise ValueError("operation must be create_folder, rename, or delete")
    arguments = {
        "device_id": device_id, "project_id": project_id,
        "operation": operation, "absolute_path": absolute_path,
        "new_name": new_name, "recursive": recursive,
    }
    user, _ = authorize_device(
        request=request, device_id=device_id,
        tool_name="dashboard_manage_device_path", write=True,
        arguments=arguments,
    )
    _authorized_project(user, device_id, project_id)
    if operation == "create_folder":
        parent_path, folder_name = str(absolute_path).rsplit("/", 1)
        if not parent_path or not folder_name:
            raise ValueError("absolute_path must name a folder below an absolute parent path")
        payload = {"parent_path": parent_path, "folder_name": folder_name}
    elif operation == "rename":
        if not str(new_name or "").strip():
            raise ValueError("new_name is required for rename")
        payload = {"old_path": str(absolute_path), "new_name": str(new_name)}
    else:
        payload = {"path": str(absolute_path), "recursive": bool(recursive)}
    result = _run_device_command(
        user=user, device_id=device_id, project_id=project_id,
        command=command, **payload,
    )
    return json.dumps(result, default=str)


def manage_device_project(*, request, device_id: int, action: str,
                          absolute_path: str | None = None,
                          project_id: int | None = None, name: str | None = None) -> str:
    """Manage server-side IDE links without conflating them with device folders."""
    action = str(action or "").strip().lower()
    if action not in {"list", "create", "delete"}:
        raise ValueError("action must be list, create, or delete")
    user, device = authorize_device(
        request=request, device_id=device_id,
        tool_name="dashboard_manage_device_project", write=action != "list",
        arguments={"device_id": device_id, "action": action,
                   "absolute_path": absolute_path, "project_id": project_id},
    )
    from data.models import Project

    projects = Project.objects.filter(user=user, device=device).order_by("name", "folder_path")
    if action == "list":
        return json.dumps({"projects": [
            {"id": row.pk, "uuid": str(row.uuid), "name": row.name,
             "absolute_path": row.folder_path, "ide_url": f"/project/{row.uuid}/"}
            for row in projects
        ]})
    if action == "create":
        path = _absolute_path(absolute_path or "")
        info = _run_device_command(
            user=user, device_id=device.pk, project_id=None,
            command="file_info", path=path,
        )
        file_info = info.get("info") if isinstance(info.get("info"), dict) else info
        is_directory = file_info.get("is_directory")
        if is_directory is None:
            is_directory = file_info.get("is_dir")
        if not is_directory:
            raise ValueError("Only an existing device folder can be marked as a project")
        project, created = Project.objects.get_or_create(
            user=user, device=device, folder_path=path,
            defaults={"name": str(name or "").strip() or None},
        )
        return json.dumps({
            "created": created, "project": {"id": project.pk, "uuid": str(project.uuid),
            "name": project.name, "absolute_path": project.folder_path,
            "ide_url": f"/project/{project.uuid}/"},
        })
    lookup = {"pk": int(project_id)} if project_id is not None else {
        "folder_path": _absolute_path(absolute_path or "")
    }
    try:
        project = projects.get(**lookup)
    except (Project.DoesNotExist, TypeError, ValueError) as exc:
        raise DashboardCodexPermissionDenied("Project link is unavailable.") from exc
    removed = {"id": project.pk, "name": project.name, "absolute_path": project.folder_path}
    project.delete()
    return json.dumps({"deleted": True, "project": removed,
                       "files_deleted": False,
                       "message": "The IDE project link was removed; device files were not deleted."})


def copy_attachment_to_device(*, request, attachment_id: str, device_id: int,
                              project_id: str | None, absolute_destination: str,
                              overwrite: bool) -> str:
    absolute_destination = _absolute_path(absolute_destination)
    user, _ = authorize_device(
        request=request, device_id=device_id,
        tool_name="dashboard_copy_attachment_to_device", write=True,
        arguments={
            "attachment_id": attachment_id, "device_id": device_id,
            "project_id": project_id, "absolute_destination": absolute_destination,
            "overwrite": overwrite,
        },
    )
    _authorized_project(user, device_id, project_id)
    chat = _request_chat(request)
    if str(attachment_id).startswith("message:"):
        message_id = str(attachment_id).removeprefix("message:")
        try:
            attachment_file = chat.messages.get(
                pk=message_id, is_outgoing=True, media__isnull=False,
            ).media
        except chat.messages.model.DoesNotExist as exc:
            raise DashboardCodexPermissionDenied("Attachment is unavailable.") from exc
        if not attachment_file.name:
            raise DashboardCodexPermissionDenied("Attachment is unavailable.")
    else:
        linked = str(attachment_id) in _chat_attachment_ids(chat, user)
        if not linked:
            raise DashboardCodexPermissionDenied("That attachment is not part of this chat.")
        from dashboard.models import DashboardCodexAttachment
        try:
            attachment = DashboardCodexAttachment.objects.get(pk=attachment_id, user=user)
        except (DashboardCodexAttachment.DoesNotExist, ValueError) as exc:
            raise DashboardCodexPermissionDenied("Attachment is unavailable.") from exc
        attachment_file = attachment.file
    from dashboard.services.path_transfers import create_browser_upload
    with attachment_file.open("rb") as uploaded:
        # FieldFile.name is the full private storage key. Passing that through
        # makes the transfer FileField repeat the directory hierarchy and can
        # exceed its max_length. Stage only the safe user-visible basename.
        staged_upload = File(uploaded, name=os.path.basename(attachment_file.name))
        job, _ = create_browser_upload(
            user=user, upload=staged_upload, destination_device_id=device_id,
            destination_path=absolute_destination, kind="file", overwrite=bool(overwrite),
        )
    return json.dumps({
        "transfer_id": str(job.pk), "status": job.status,
        "payload_size": job.payload_size, "progress_percent": float(job.progress_percent),
        "message": "Attachment transfer queued and will resume automatically after reconnects.",
    })


def start_device_path_transfer(*, request, source_device_id: int, source_path: str,
                               destination_device_id: int, destination_path: str,
                               kind: str, overwrite: bool, tool_call=None,
                               wait_for_completion: bool = True) -> str | None:
    source_path = _absolute_path(source_path)
    destination_path = _absolute_path(destination_path)
    arguments = {
        "source_device_id": source_device_id, "source_path": source_path,
        "destination_device_id": destination_device_id, "destination_path": destination_path,
        "kind": kind, "overwrite": overwrite,
    }
    source_user, _ = authorize_device(
        request=request, device_id=source_device_id,
        tool_name="dashboard_start_device_path_transfer", write=False, arguments=arguments,
    )
    destination_user, _ = authorize_device(
        request=request, device_id=destination_device_id,
        tool_name="dashboard_start_device_path_transfer", write=True, arguments=arguments,
    )
    if source_user.pk != destination_user.pk:
        raise DashboardCodexPermissionDenied("Both devices must belong to the Dashboard Codex user.")
    from dashboard.services.path_transfers import create_offer
    job, _ = create_offer(
        user=source_user, source_device_id=source_device_id, source_path=source_path,
        kind=kind, destination_device_id=destination_device_id,
        destination_path=destination_path, overwrite=bool(overwrite),
        lifetime_seconds=24 * 60 * 60,
    )
    metadata = dict(job.transfer_metadata or {})
    if tool_call:
        metadata["dashboard_ai_call_id"] = tool_call.call_id
        metadata["dashboard_ai_wait_for_completion"] = bool(wait_for_completion)
        job.transfer_metadata = metadata
        job.save(update_fields=["transfer_metadata", "updated_at"])
    result = {"transfer_id": str(job.pk), "status": job.status, "progress_percent": 0,
              "wait_for_completion": bool(wait_for_completion)}
    return None if tool_call and wait_for_completion else json.dumps(result)


def _authorized_transfer_job(*, request, transfer_id: str, write_destination: bool,
                             tool_name: str):
    user = _request_user(request)
    from dashboard.models import PathTransferJob
    try:
        job = PathTransferJob.objects.get(pk=transfer_id, source_user=user, destination_user=user)
    except (PathTransferJob.DoesNotExist, ValueError) as exc:
        raise DashboardCodexPermissionDenied("Transfer is unavailable.") from exc
    authorize_device(request=request, device_id=job.source_device_id,
                     tool_name=tool_name, write=False,
                     arguments={"transfer_id": transfer_id})
    if job.destination_device_id:
        authorize_device(request=request, device_id=job.destination_device_id,
                         tool_name=tool_name, write=write_destination,
                         arguments={"transfer_id": transfer_id})
    return job


def get_device_path_transfer(*, request, transfer_id: str) -> str:
    job = _authorized_transfer_job(
        request=request, transfer_id=transfer_id, write_destination=False,
        tool_name="dashboard_get_device_path_transfer",
    )
    return json.dumps({
        "transfer_id": str(job.pk), "status": job.status,
        "payload_size": job.payload_size, "completed_bytes": job.completed_bytes,
        "progress_percent": float(job.progress_percent), "error": job.error,
        "source_wire_bytes": job.source_wire_bytes,
        "destination_wire_bytes": job.destination_wire_bytes,
        "source_peak_additional_bytes": job.source_peak_additional_bytes,
        "destination_peak_additional_bytes": job.destination_peak_additional_bytes,
    })


def cancel_device_path_transfer(*, request, transfer_id: str) -> str:
    job = _authorized_transfer_job(
        request=request, transfer_id=transfer_id, write_destination=True,
        tool_name="dashboard_cancel_device_path_transfer",
    )
    from dashboard.services.path_transfers import cancel
    from dashboard.services.codex_transfers import respond_to_waiting_ai_transfer
    from data.services.utils import sync_run_async
    job = sync_run_async(cancel(job.pk, job.get_authorization_token()))
    respond_to_waiting_ai_transfer(job.pk)
    return json.dumps({"transfer_id": str(job.pk), "status": job.status})


def set_device_exposed_ports(*, request, device_id: int, ports: list[Any]) -> str:
    user, _ = authorize_device(
        request=request, device_id=device_id,
        tool_name="dashboard_set_device_exposed_ports", write=True,
        arguments={"device_id": device_id, "ports": ports},
    )
    from dashboard.views.device_container_views import (
        ExposePortsApplyError,
        _normalize_port_list,
        apply_expose_ports_for_device,
    )
    try:
        normalized = _normalize_port_list(ports)
        result = apply_expose_ports_for_device(user, device_id, normalized, broadcast=True)
    except ExposePortsApplyError as exc:
        raise RuntimeError(
            f"{exc} Re-read the device with dashboard_list_devices before reporting failure; "
            "the canonical exposure operation may have completed after this timeout."
        ) from exc
    public_links = [
        {
            "url": service["url"],
            "label": f"Open port {service.get('port', '?')}",
            "port": service.get("port"),
        }
        for service in result.get("exposed_ports", [])
        if isinstance(service, dict) and isinstance(service.get("url"), str)
    ]
    if public_links:
        result["_unicom_presentation"] = {
            "type": "public_links",
            "links": public_links,
        }
        result["application_environment_hint"] = (
            "Inside the container, use PORTACODE_PRIMARY_PUBLIC_HOST for host "
            "allowlists such as Django ALLOWED_HOSTS, and "
            "PORTACODE_PRIMARY_PUBLIC_URL for full-origin settings such as "
            "Django CSRF_TRUSTED_ORIGINS and external/base URLs. For multiple "
            "services, read PORTACODE_EXPOSED_SERVICES_JSON. Prefer these "
            "variables over hardcoding the domain."
        )
    return json.dumps(result, default=str)


def connect_domain(*, request, device_id: int) -> str:
    user, _ = authorize_device(request=request, device_id=device_id,
        tool_name="dashboard_connect_device_domain", write=True)
    from dashboard.services.device_domains import connect_device_domain
    result = connect_device_domain(actor=user, device_id=device_id)
    result["_unicom_presentation"] = {
        "type": "domain_connection", "status": result["status"],
        "device_id": result["device_id"], "device_name": result["device_name"],
        "domain": (result.get("cloudflare_tunnel") or {}).get("domain", ""),
        "login_url": result.get("login_url", ""),
    }
    return json.dumps(result, default=str)


def disconnect_domain(*, request, device_id: int) -> str:
    user, _ = authorize_device(request=request, device_id=device_id,
        tool_name="dashboard_disconnect_device_domain", write=True)
    from dashboard.services.device_domains import disconnect_device_domain
    result = disconnect_device_domain(actor=user, device_id=device_id)
    result["_unicom_presentation"] = {
        "type": "domain_disconnect", "device_id": result["device_id"],
        "device_name": result["device_name"], "domain": result.get("domain", ""),
        "message": result.get("message", ""), "success": True,
    }
    return json.dumps(result, default=str)


def manage_domain_ingress(*, request, device_id: int, rules: list[Any]) -> str:
    user, _ = authorize_device(request=request, device_id=device_id,
        tool_name="dashboard_manage_device_ingress", write=True,
        arguments={"device_id": device_id, "rule_count": len(rules) if isinstance(rules, list) else None})
    from dashboard.services.device_domains import configure_device_ingress
    result = configure_device_ingress(actor=user, device_id=device_id, rules=rules)
    result["_unicom_presentation"] = {
        "type": "ingress_rules", "device_id": result["device_id"],
        "device_name": result["device_name"], "domain": result.get("domain", ""),
        "rules": result.get("rules", []), "success": True,
    }
    return json.dumps(result, default=str)


def terminal_exec(*, request, device_id: int, project_id: str | None,
                  command: str, timeout_seconds: int) -> str:
    """Execute through the existing authenticated device client and await its result."""
    arguments = {
        "device_id": device_id,
        "project_id": project_id,
        "command": command,
        "timeout_seconds": timeout_seconds,
    }
    user, _ = authorize_device(
        request=request,
        device_id=device_id,
        tool_name="dashboard_terminal_exec",
        write=True,
        arguments=arguments,
    )
    command = str(command or "").strip()
    if not command:
        raise ValueError("command is required")
    timeout_seconds = min(max(int(timeout_seconds), 1), 900)
    if project_id:
        import uuid
        from data.models import Project
        try:
            project_uuid = uuid.UUID(str(project_id))
        except (TypeError, ValueError) as exc:
            raise DashboardCodexPermissionDenied("Invalid project identifier.") from exc
        if not Project.objects.filter(uuid=project_uuid, user=user, device_id=device_id).exists():
            raise DashboardCodexPermissionDenied(
                "The project does not belong to the authorized device and user."
            )

    from data.services.device_client import DeviceServiceClient
    from data.services.utils import sync_run_async

    async def execute():
        client = DeviceServiceClient(user_id=user.pk, project_id=project_id or None)
        if not await client.connect():
            return {"error": "Unable to connect to the authenticated device gateway."}
        request_id = client._generate_request_id()
        try:
            future = asyncio.Future()
            future.expected_event = "terminal_exec_result"
            future.device_id = int(device_id)
            future.sent_request_id = request_id
            future.require_request_id = True
            client.response_futures[request_id] = future
            payload = {
                "device_id": int(device_id),
                "channel": 0,
                "payload": {
                    "cmd": "terminal_exec",
                    "command": command,
                    "request_id": request_id,
                },
            }
            if project_id:
                payload["payload"]["project_id"] = project_id
            await client.websocket.send(json.dumps(payload))
            try:
                response = await asyncio.wait_for(future, timeout=timeout_seconds)
            except asyncio.TimeoutError:
                return {
                    "command": command,
                    "timed_out": True,
                    "timeout_seconds": timeout_seconds,
                }
            return {
                "command": command,
                "exit_code": response.get("returncode"),
                "stdout": str(response.get("stdout") or "")[:20000],
                "stderr": str(response.get("stderr") or "")[:20000],
                "timed_out": False,
                "duration_seconds": response.get("duration_s"),
            }
        finally:
            client.response_futures.pop(request_id, None)
            await client.disconnect()

    result = sync_run_async(execute())
    if result.get("error"):
        raise RuntimeError(result["error"])
    result["_unicom_presentation"] = {
        "type": "terminal",
        "command": command,
        "exit_code": result.get("exit_code"),
        "stdout": str(result.get("stdout") or "")[-6000:],
        "stderr": str(result.get("stderr") or "")[-3000:],
        "timed_out": bool(result.get("timed_out")),
        "duration_seconds": result.get("duration_seconds"),
    }
    return json.dumps(result, default=str)


def run_device_codex_task(*, request, device_id: int, project_id: str,
                          task: str, response_word_limit: int,
                          timeout_seconds: int, model: str | None = None) -> str:
    """Run one synchronous, request-scoped Codex job in an authorized project."""
    arguments = {
        "device_id": device_id, "project_id": project_id,
        "task": str(task or "")[:500],
        "response_word_limit": response_word_limit,
        "timeout_seconds": timeout_seconds, "model": model,
    }
    user, device = authorize_device(
        request=request, device_id=device_id,
        tool_name="dashboard_run_device_codex_task", write=True,
        arguments=arguments,
    )
    from dashboard.services.codex_context import (
        DASHBOARD_DEVICE_CODEX_MIN_VERSION,
        device_supports_dashboard_codex,
    )
    if not device_supports_dashboard_codex(device):
        minimum = ".".join(str(part) for part in DASHBOARD_DEVICE_CODEX_MIN_VERSION)
        raise DashboardCodexPermissionDenied(
            f"Dashboard Codex tasks require Portacode {minimum} or later on this device."
        )
    task = str(task or "").strip()
    if not task:
        raise ValueError("task is required")
    if len(task) > 12000:
        raise ValueError("task must be 12,000 characters or fewer")
    project = _authorized_project(user, device.pk, project_id)
    if project is None or not project.folder_path:
        raise DashboardCodexPermissionDenied("A project on the authorized device is required.")
    initial = request.initial_request or request
    chat = _request_chat(request)
    word_limit = min(max(int(response_word_limit), 40), 400)
    timeout = min(max(int(timeout_seconds), 30), 1800)
    result = _run_device_command(
        user=user, device_id=device.pk, project_id=str(project.uuid),
        command="codex_task_execute", timeout=float(timeout + 30),
        expected_event="codex_task_result",
        cwd=project.folder_path, task=task,
        response_word_limit=word_limit, timeout_seconds=timeout,
        model=str(model or "").strip() or None,
        dashboard_chat_id=str(chat.pk), dashboard_request_id=str(initial.pk),
    )
    presentation = {
        "type": "codex_task",
        "success": bool(result.get("success")),
        "timed_out": bool(result.get("timed_out")),
        "device_id": device.pk,
        "device_name": device.name,
        "project_id": str(project.uuid),
        "project_name": project.name,
        "summary": str(result.get("summary") or "")[:16000],
        "thread_id": str(result.get("thread_id") or "")[:200],
        "exit_code": result.get("exit_code"),
        "response_word_limit": word_limit,
    }
    result["_unicom_presentation"] = presentation
    # The tool result intentionally excludes raw JSONL. Usage accounting is
    # authoritative in CodexUsageEvent and is exposed by the thread ledger.
    return json.dumps(result, default=str)


def start_device_automation(*, request, device_id: int, automation_yaml: str,
                            input_values: dict[str, Any] | None = None) -> str:
    """Create a normal durable AutomationTask on an already-authorized device."""
    arguments = {"device_id": device_id, "automation_yaml": "[redacted]"}
    user, device = authorize_device(
        request=request, device_id=device_id,
        tool_name="dashboard_start_device_automation", write=True,
        arguments=arguments,
    )
    from django.db import transaction
    from django.http import QueryDict
    from data.models import AutomationTask
    from dashboard.views.device_container_views import (
        _build_automation_task_yaml,
        _normalize_automation_behavior_metadata,
        _normalize_template_input_definitions,
        _normalize_template_input_submission,
        _parse_automation_task_definition,
        _persist_automation_task_input_values,
        _serialize_automation_task,
    )

    instructions, metadata = _parse_automation_task_definition(automation_yaml)
    metadata = _normalize_automation_behavior_metadata(metadata)
    if not instructions and not metadata.get("expose_ports"):
        raise ValueError("Provide at least one instruction or metadata.expose_ports")
    definitions = _normalize_template_input_definitions(automation_yaml)
    values, _save_ids = _normalize_template_input_submission(
        input_values or {}, [], definitions, QueryDict(),
    )
    persisted = [row["id"] for row in definitions if row.get("persist_environment")]
    if persisted:
        metadata["persist_environment_inputs"] = persisted
    metadata.setdefault("on_failure", "auto_fix")
    metadata["dashboard_ai"] = {
        "origin": "dashboard_codex", "chat_id": _request_chat(request).pk,
        "recovery_attempts": 0, "max_recovery_attempts": 3,
        "suppress_failure_notification": True,
    }
    with transaction.atomic():
        if AutomationTask.objects.select_for_update().filter(
            device=device, status__in=(AutomationTask.STATUS_PENDING, AutomationTask.STATUS_RUNNING),
        ).exists():
            raise RuntimeError("The device already has an active automation task.")
        task = AutomationTask.objects.create(
            device=device, instructions=instructions, metadata=metadata,
            original_yaml=_build_automation_task_yaml(instructions, metadata, raw_text=automation_yaml),
        )
        _persist_automation_task_input_values(task=task, definitions=definitions, values=values)
    return json.dumps({"task": _serialize_automation_task(task)}, default=str)


def manage_device_automation(*, request, device_id: int, action: str,
                             automation_task_id: int | None = None) -> str:
    action = str(action or "").strip().lower()
    if action not in {"list", "state", "cancel", "retry", "restart"}:
        raise ValueError("action must be list, state, cancel, retry, or restart")
    write = action in {"cancel", "retry", "restart"}
    user, device = authorize_device(
        request=request, device_id=device_id,
        tool_name="dashboard_manage_device_automation", write=write,
        arguments={"device_id": device_id, "action": action,
                   "automation_task_id": automation_task_id},
    )
    from django.db import transaction
    from django.utils import timezone
    from data.models import AutomationTask
    from dashboard.views.device_container_views import _serialize_automation_task

    rows = AutomationTask.objects.filter(device=device).order_by("-created_at")
    if action == "list":
        return json.dumps({"tasks": [_serialize_automation_task(row) for row in rows[:20]]}, default=str)
    if automation_task_id is None:
        raise ValueError("automation_task_id is required for this action")
    try:
        task = rows.get(pk=int(automation_task_id))
    except (AutomationTask.DoesNotExist, TypeError, ValueError) as exc:
        raise DashboardCodexPermissionDenied("Automation task is unavailable.") from exc
    if action == "state":
        return json.dumps({"task": _serialize_automation_task(task)}, default=str)

    if action == "cancel":
        if not task.is_active:
            raise ValueError("Only a pending or running automation can be canceled")
        if task.status == AutomationTask.STATUS_RUNNING:
            _run_device_command(
                user=user, device_id=device.pk, project_id=None,
                command="automation_v2_cancel", task_id=str(task.pk), timeout=15,
                expected_event="automation_v2_cancelled",
            )
        with transaction.atomic():
            task = AutomationTask.objects.select_for_update().get(pk=task.pk)
            task.status = AutomationTask.STATUS_CANCELLED
            task.finished_at = timezone.now()
            task.save(update_fields=["status", "finished_at", "updated_at"])
    else:
        allowed = {AutomationTask.STATUS_FAILED, AutomationTask.STATUS_CANCELLED}
        if task.status not in allowed:
            raise ValueError("Only failed or canceled automation can be retried or restarted")
        if AutomationTask.objects.filter(
            device=device, status__in=(AutomationTask.STATUS_PENDING, AutomationTask.STATUS_RUNNING),
        ).exclude(pk=task.pk).exists():
            raise RuntimeError("The device already has another active automation task.")
        metadata = dict(task.metadata or {})
        metadata.pop("failure_details", None)
        metadata.pop("expose_ports_progress", None)
        if action == "restart":
            task.current_step_index = 0
        task.status = AutomationTask.STATUS_PENDING
        task.current_step_status = AutomationTask.STEP_STATUS_PENDING
        task.metadata = metadata
        task.last_error = None
        task.started_at = None
        task.finished_at = None
        task.save(update_fields=[
            "status", "current_step_index", "current_step_status", "metadata",
            "last_error", "started_at", "finished_at", "updated_at",
        ])
    return json.dumps({"action": action, "task": _serialize_automation_task(task)}, default=str)


def offer_device_upload(*, request, device_id: int, destination_folder: str,
                        allow_files: bool, allow_folders: bool, overwrite: bool) -> str:
    destination_folder = _absolute_path(destination_folder)
    if not allow_files and not allow_folders:
        raise ValueError("At least one of allow_files or allow_folders must be enabled")
    authorize_device(
        request=request, device_id=device_id, tool_name="dashboard_offer_device_upload",
        write=True, arguments={"device_id": device_id, "destination_folder": destination_folder},
    )
    return json.dumps({"ui": "device_upload", "device_id": device_id,
                       "destination_folder": destination_folder,
                       "allow_files": bool(allow_files), "allow_folders": bool(allow_folders),
                       "overwrite": bool(overwrite)})


def offer_device_download(*, request, device_id: int, source_path: str, kind: str) -> str:
    source_path = _absolute_path(source_path)
    if kind not in {"file", "folder"}:
        raise ValueError("kind must be file or folder")
    authorize_device(
        request=request, device_id=device_id, tool_name="dashboard_offer_device_download",
        write=False, arguments={"device_id": device_id, "source_path": source_path, "kind": kind},
    )
    return json.dumps({
        "ui": "device_download", "device_id": device_id,
        "source_path": source_path, "kind": kind,
        "state": "awaiting_user_start",
        "instruction": (
            "The download control is displayed, but no transfer has started. "
            "Do not say the item is ready to download; preparation and any errors appear in the control."
        ),
    })


def provision_device(*, request, tool_call, arguments: dict[str, Any]) -> str:
    """Ask the ASGI singleton to run the canonical provisioning service."""
    user = _request_user(request)
    from dashboard.services.codex_permissions import require_container_provisioning
    require_container_provisioning(user=user)
    token = getattr(settings, "UNICOM_STREAM_RELAY_TOKEN", "") or ""
    if not token:
        raise RuntimeError("The internal action relay is not configured.")
    url = (
        getattr(settings, "UNICOM_ACTION_RELAY_INTERNAL_URL", "")
        or "http://django:8001/internal/unicom/action/"
    )
    response = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "request_id": str(request.pk),
            "user_id": user.pk,
            "action": "provision_device",
            "tool_call_id": tool_call.call_id,
            "arguments": arguments,
        },
        timeout=30.0,
    )
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("The provisioning service returned an invalid response.") from exc
    if not response.is_success:
        detail = result.get("details")
        message = str(result.get("error") or "Provisioning failed.")
        raise RuntimeError(f"{message}: {detail}" if detail else message)
    return json.dumps(result, default=str)
