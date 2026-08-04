"""Disk-backed, reconnectable file and folder transfers.

The transport remains the authenticated Portacode control WebSocket.  Transfer
state and partial payloads live on disk so a socket or agent restart does not
force the transfer to begin again.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import shutil
import stat
import tarfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable

from .base import SyncHandler


DEFAULT_CHUNK_SIZE = 192 * 1024
MIN_CHUNK_SIZE = 32 * 1024
MAX_CHUNK_SIZE = 1024 * 1024
TRANSFER_TTL_SECONDS = 24 * 60 * 60
STORAGE_SAFETY_BYTES = 16 * 1024 * 1024
MAX_TRANSFER_PAYLOAD_BYTES = int(os.environ.get("PORTACODE_TRANSFER_MAX_PAYLOAD_BYTES", 10 * 1024 * 1024 * 1024))
MAX_TRANSFER_ENTRIES = int(os.environ.get("PORTACODE_TRANSFER_MAX_ENTRIES", 100_000))


def _cache_root() -> Path:
    override = os.environ.get("PORTACODE_TRANSFER_CACHE_DIR")
    root = Path(override) if override else Path.home() / ".cache" / "portacode" / "transfers"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _transfer_id(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError("transfer_id must be a UUID") from exc


def _authorization_token(value: Any) -> str:
    token = str(value or "")
    if len(token) < 32:
        raise ValueError("authorization_token must contain at least 32 characters")
    return token


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _job_dir(transfer_id: str) -> Path:
    return _cache_root() / transfer_id


def _manifest_path(transfer_id: str) -> Path:
    return _job_dir(transfer_id) / "manifest.json"


def _write_manifest(manifest: Dict[str, Any]) -> None:
    path = _manifest_path(manifest["transfer_id"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _load_manifest(transfer_id: Any, token: Any) -> Dict[str, Any]:
    transfer_id = _transfer_id(transfer_id)
    token = _authorization_token(token)
    path = _manifest_path(transfer_id)
    if not path.is_file():
        raise ValueError("Transfer does not exist or has expired")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not hmac.compare_digest(str(manifest.get("authorization_hash") or ""), _token_hash(token)):
        raise PermissionError("Transfer authorization is invalid")
    if float(manifest.get("expires_at") or 0) <= time.time():
        raise ValueError("Transfer authorization has expired")
    return manifest


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    if not current.exists():
        raise ValueError(f"No existing parent filesystem for {path}")
    return current if current.is_dir() else current.parent


def _free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(_nearest_existing_parent(path)).free)


def _filesystem_block_size(path: Path) -> int:
    details = os.statvfs(_nearest_existing_parent(path))
    return max(int(details.f_frsize or details.f_bsize or 4096), 512)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_metadata(path: Path) -> Dict[str, Any]:
    info = path.lstat()
    return {
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mtime_ns": info.st_mtime_ns,
        "atime_ns": info.st_atime_ns,
    }


def _folder_metrics(path: Path) -> Dict[str, int]:
    logical = 0
    allocated = 0
    entries = 1
    for root, directories, files in os.walk(path, followlinks=False):
        entries += len(directories) + len(files)
        for name in directories:
            item = Path(root) / name
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"Folder transfers do not support symbolic links: {item}")
        for name in files:
            item = Path(root) / name
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"Folder transfers do not support symbolic links: {item}")
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Folder transfers do not support special files: {item}")
            logical += info.st_size
            allocated += max(info.st_blocks * 512, info.st_size)
    return {"expanded_size": logical, "expanded_storage_bytes": allocated, "entry_count": entries}


def _estimated_tar_bytes(metrics: Dict[str, int]) -> int:
    # PAX headers vary with path and metadata. This is deliberately
    # conservative and is checked again against the archive's actual size.
    data = int(metrics["expanded_storage_bytes"])
    entries = int(metrics["entry_count"])
    return data + entries * 4096 + 2 * 512 + 1024 * 1024


def _storage_error(*, side: str, path: Path, required: int, available: int,
                   archive: int, extraction: int) -> ValueError:
    deficit = max(required - available, 0)
    return ValueError(
        f"Insufficient free space on the {side} filesystem for {path}: "
        f"required {required} bytes (archive={archive}, extraction={extraction}, "
        f"safety={STORAGE_SAFETY_BYTES}), available {available} bytes, "
        f"short by {deficit} bytes."
    )


def _public_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    hidden = {"authorization_hash", "payload_path", "partial_path"}
    result = {key: value for key, value in manifest.items() if key not in hidden}
    total = max(int(manifest.get("payload_size") or 0), 1)
    completed = min(int(manifest.get("completed_bytes") or 0), total)
    result["progress_percent"] = round(completed * 100 / total, 2)
    result["complete"] = manifest.get("state") == "completed"
    return result


def _cleanup_expired() -> None:
    now = time.time()
    for child in _cache_root().iterdir():
        if not child.is_dir():
            continue
        try:
            manifest = json.loads((child / "manifest.json").read_text(encoding="utf-8"))
            expired = float(manifest.get("expires_at") or 0) <= now
        except Exception:
            manifest = None
            expired = child.stat().st_mtime < now - TRANSFER_TTL_SECONDS
        if expired:
            if manifest:
                _cleanup_destination_staging(manifest)
            shutil.rmtree(child, ignore_errors=True)


def _cleanup_destination_staging(manifest: Dict[str, Any]) -> None:
    """Remove only UUID-bound staging names outside the cache directory."""
    transfer_id = str(manifest.get("transfer_id") or "")
    expected = {
        f".portacode-incoming-{transfer_id}.part": "file",
        f".portacode-staged-{transfer_id}": "file",
        f".portacode-extract-{transfer_id}": "directory",
    }
    for key in ("partial_path", "staged_path", "extraction_path"):
        raw = manifest.get(key)
        if not raw:
            continue
        path = Path(str(raw))
        kind = expected.get(path.name)
        try:
            if kind == "directory":
                shutil.rmtree(path, ignore_errors=True)
            elif kind == "file":
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _prepare_source(message: Dict[str, Any]) -> Dict[str, Any]:
    transfer_id = _transfer_id(message.get("transfer_id"))
    token = _authorization_token(message.get("authorization_token"))
    source = Path(str(message.get("path") or "")).expanduser().resolve()
    if not source.exists():
        raise ValueError(f"Source path does not exist: {source}")
    kind = "folder" if source.is_dir() else "file" if source.is_file() else ""
    requested_kind = str(message.get("kind") or kind)
    if not kind or requested_kind != kind:
        raise ValueError(f"Source is not a {requested_kind}: {source}")
    chunk_size = min(max(int(message.get("chunk_size") or DEFAULT_CHUNK_SIZE), MIN_CHUNK_SIZE), MAX_CHUNK_SIZE)
    job_dir = _job_dir(transfer_id)
    if job_dir.exists():
        existing = _load_manifest(transfer_id, token)
        if existing.get("role") != "source" or existing.get("source_path") != str(source):
            raise ValueError("transfer_id is already bound to another operation")
        return _public_manifest(existing)
    job_dir.mkdir(parents=True, mode=0o700)

    source_extra_required = 0
    metrics = {"expanded_size": source.stat().st_size, "expanded_storage_bytes": source.stat().st_size, "entry_count": 1}
    payload = source
    archive_format = None
    if kind == "file" and source.stat().st_size > MAX_TRANSFER_PAYLOAD_BYTES:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise ValueError(f"Transfer payload exceeds the {MAX_TRANSFER_PAYLOAD_BYTES} byte limit")
    if kind == "folder":
        metrics = _folder_metrics(source)
        if metrics["entry_count"] > MAX_TRANSFER_ENTRIES:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise ValueError(f"Folder exceeds the {MAX_TRANSFER_ENTRIES} entry limit")
        estimate = _estimated_tar_bytes(metrics)
        if estimate > MAX_TRANSFER_PAYLOAD_BYTES:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise ValueError(f"Estimated folder archive exceeds the {MAX_TRANSFER_PAYLOAD_BYTES} byte limit")
        available = _free_bytes(job_dir)
        required = estimate + STORAGE_SAFETY_BYTES
        if available < required:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise _storage_error(side="source", path=job_dir, required=required,
                                 available=available, archive=estimate, extraction=0)
        payload = job_dir / "payload.tar"
        try:
            with tarfile.open(payload, "w", format=tarfile.PAX_FORMAT, dereference=False) as archive:
                archive.add(source, arcname=source.name, recursive=True)
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        source_extra_required = payload.stat().st_size
        if source_extra_required > MAX_TRANSFER_PAYLOAD_BYTES:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise ValueError(f"Transfer payload exceeds the {MAX_TRANSFER_PAYLOAD_BYTES} byte limit")
        archive_format = "tar"

    payload_size = payload.stat().st_size
    manifest = {
        "protocol_version": 1,
        "transfer_id": transfer_id,
        "authorization_hash": _token_hash(token),
        "role": "source",
        "kind": kind,
        "state": "ready",
        "source_path": str(source),
        "name": source.name,
        "payload_path": str(payload),
        "payload_size": payload_size,
        "payload_hash": _hash_file(payload),
        "chunk_size": chunk_size,
        "chunk_count": max(1, math.ceil(payload_size / chunk_size)),
        "completed_chunks": [],
        "completed_bytes": 0,
        "expanded_size": metrics["expanded_size"],
        "expanded_storage_bytes": metrics["expanded_storage_bytes"],
        "entry_count": metrics["entry_count"],
        "metadata": _file_metadata(source),
        "archive_format": archive_format,
        "source_peak_additional_bytes": source_extra_required,
        "created_at": time.time(),
        "expires_at": time.time() + min(max(int(message.get("ttl_seconds") or TRANSFER_TTL_SECONDS), 60), TRANSFER_TTL_SECONDS),
    }
    _write_manifest(manifest)
    return _public_manifest(manifest)


def _prepare_destination(message: Dict[str, Any]) -> Dict[str, Any]:
    transfer_id = _transfer_id(message.get("transfer_id"))
    token = _authorization_token(message.get("authorization_token"))
    destination = Path(str(message.get("path") or "")).expanduser().resolve()
    kind = str(message.get("kind") or "")
    if kind not in {"file", "folder"}:
        raise ValueError("kind must be file or folder")
    payload_size = int(message.get("payload_size") or -1)
    expanded_storage = int(message.get("expanded_storage_bytes") or payload_size)
    entry_count = int(message.get("entry_count") or 1)
    if payload_size < 0 or expanded_storage < 0:
        raise ValueError("payload_size and expanded_storage_bytes must be non-negative")
    if payload_size > MAX_TRANSFER_PAYLOAD_BYTES:
        raise ValueError(f"Transfer payload exceeds the {MAX_TRANSFER_PAYLOAD_BYTES} byte limit")
    if entry_count < 1 or entry_count > MAX_TRANSFER_ENTRIES:
        raise ValueError(f"entry_count must be between 1 and {MAX_TRANSFER_ENTRIES}")
    if destination.exists() and not bool(message.get("overwrite", False)):
        raise ValueError(f"Destination already exists: {destination}")
    job_dir = _job_dir(transfer_id)
    if job_dir.exists():
        existing = _load_manifest(transfer_id, token)
        if existing.get("role") != "destination" or existing.get("destination_path") != str(destination):
            raise ValueError("transfer_id is already bound to another operation")
        return _public_manifest(existing)
    job_dir.mkdir(parents=True, mode=0o700)
    entry_overhead = entry_count * _filesystem_block_size(destination) if kind == "folder" else 0
    extraction = expanded_storage + entry_overhead if kind == "folder" else 0
    required = payload_size + extraction + STORAGE_SAFETY_BYTES
    available = _free_bytes(destination)
    if available < required:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise _storage_error(side="destination", path=destination, required=required,
                             available=available, archive=payload_size, extraction=extraction)
    staging_parent = _nearest_existing_parent(destination)
    partial = staging_parent / f".portacode-incoming-{transfer_id}.part"
    with partial.open("wb") as stream:
        stream.truncate(payload_size)
    manifest = {
        "protocol_version": 1,
        "transfer_id": transfer_id,
        "authorization_hash": _token_hash(token),
        "role": "destination",
        "kind": kind,
        "state": "receiving",
        "destination_path": str(destination),
        "partial_path": str(partial),
        "staged_path": str(staging_parent / f".portacode-staged-{transfer_id}"),
        "extraction_path": str(staging_parent / f".portacode-extract-{transfer_id}"),
        "payload_size": payload_size,
        "payload_hash": str(message.get("payload_hash") or ""),
        "chunk_size": min(max(int(message.get("chunk_size") or DEFAULT_CHUNK_SIZE), MIN_CHUNK_SIZE), MAX_CHUNK_SIZE),
        "chunk_count": int(message.get("chunk_count") or max(1, math.ceil(payload_size / DEFAULT_CHUNK_SIZE))),
        "completed_chunks": [],
        "completed_bytes": 0,
        "expanded_size": int(message.get("expanded_size") or expanded_storage),
        "expanded_storage_bytes": expanded_storage,
        "entry_count": entry_count,
        "destination_entry_overhead_bytes": entry_overhead,
        "metadata": message.get("metadata") if isinstance(message.get("metadata"), dict) else {},
        "archive_format": message.get("archive_format"),
        "overwrite": bool(message.get("overwrite", False)),
        "destination_peak_additional_bytes": payload_size + extraction,
        "storage_required_bytes": required,
        "storage_available_bytes": available,
        "created_at": time.time(),
        "expires_at": time.time() + min(max(int(message.get("ttl_seconds") or TRANSFER_TTL_SECONDS), 60), TRANSFER_TTL_SECONDS),
    }
    _write_manifest(manifest)
    return _public_manifest(manifest)


def _validate_chunk_index(manifest: Dict[str, Any], value: Any) -> int:
    index = int(value)
    if index < 0 or index >= int(manifest["chunk_count"]):
        raise ValueError("chunk_index is outside this transfer")
    return index


def _read_chunk(message: Dict[str, Any]) -> Dict[str, Any]:
    manifest = _load_manifest(message.get("transfer_id"), message.get("authorization_token"))
    if manifest.get("role") != "source" or manifest.get("state") not in {"ready", "transferring"}:
        raise ValueError("Transfer is not a readable source")
    index = _validate_chunk_index(manifest, message.get("chunk_index"))
    offset = index * int(manifest["chunk_size"])
    with Path(manifest["payload_path"]).open("rb") as stream:
        stream.seek(offset)
        raw = stream.read(int(manifest["chunk_size"]))
    if not raw and int(manifest["payload_size"]) != 0:
        raise ValueError("Source payload changed or is no longer available")
    completed = set(int(value) for value in manifest.get("completed_chunks", []))
    completed.add(index)
    manifest["completed_chunks"] = sorted(completed)
    manifest["completed_bytes"] = sum(
        min(int(manifest["chunk_size"]), max(int(manifest["payload_size"]) - item * int(manifest["chunk_size"]), 0))
        for item in completed
    )
    manifest["state"] = "transferring"
    _write_manifest(manifest)
    return {
        "event": "transfer_chunk_response",
        **_public_manifest(manifest),
        "chunk_index": index,
        "chunk_size": len(raw),
        "chunk_hash": hashlib.sha256(raw).hexdigest(),
        "content_base64": base64.b64encode(raw).decode("ascii"),
    }


def _receive_chunk(message: Dict[str, Any]) -> Dict[str, Any]:
    manifest = _load_manifest(message.get("transfer_id"), message.get("authorization_token"))
    if manifest.get("role") != "destination" or manifest.get("state") != "receiving":
        raise ValueError("Transfer is not receiving chunks")
    index = _validate_chunk_index(manifest, message.get("chunk_index"))
    try:
        raw = base64.b64decode(str(message.get("content_base64") or ""), validate=True)
    except Exception as exc:
        raise ValueError("Chunk is not valid base64") from exc
    expected_size = min(
        int(manifest["chunk_size"]),
        max(int(manifest["payload_size"]) - index * int(manifest["chunk_size"]), 0),
    )
    if len(raw) != expected_size:
        raise ValueError(f"Chunk size mismatch: expected {expected_size}, got {len(raw)}")
    expected_hash = str(message.get("chunk_hash") or "")
    actual_hash = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(expected_hash, actual_hash):
        raise ValueError("Chunk hash mismatch")
    completed = set(int(value) for value in manifest.get("completed_chunks", []))
    if index not in completed:
        with Path(manifest["partial_path"]).open("r+b") as stream:
            stream.seek(index * int(manifest["chunk_size"]))
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        completed.add(index)
        manifest["completed_chunks"] = sorted(completed)
        manifest["completed_bytes"] = sum(
            min(int(manifest["chunk_size"]), max(int(manifest["payload_size"]) - item * int(manifest["chunk_size"]), 0))
            for item in completed
        )
        _write_manifest(manifest)
    return {"event": "transfer_chunk_received", **_public_manifest(manifest), "chunk_index": index}


def _safe_members(archive: tarfile.TarFile, root: Path) -> Iterable[tarfile.TarInfo]:
    root_resolved = root.resolve()
    for member in archive.getmembers():
        pure = PurePosixPath(member.name)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise ValueError(f"Unsafe archive path: {member.name}")
        destination = (root / Path(*pure.parts)).resolve()
        if os.path.commonpath((str(root_resolved), str(destination))) != str(root_resolved):
            raise ValueError(f"Archive path escapes destination: {member.name}")
        if member.ischr() or member.isblk() or member.isfifo():
            raise ValueError(f"Unsupported special file in archive: {member.name}")
        # Reject links entirely.  Merely checking each link target is not
        # sufficient because a later member can traverse a symlink created by
        # an earlier member during extraction.
        if member.issym() or member.islnk():
            raise ValueError(f"Archive links are forbidden: {member.name}")
        yield member


def _apply_metadata(path: Path, metadata: Dict[str, Any]) -> Dict[str, bool]:
    result = {"mode": False, "ownership": False, "timestamps": False}
    try:
        os.chmod(path, int(metadata["mode"]), follow_symlinks=False)
        result["mode"] = True
    except (OSError, NotImplementedError):
        # Some Unix builds expose follow_symlinks but do not implement it for
        # chmod. Staged file payloads are verified regular files.
        if path.is_file() and not path.is_symlink():
            try:
                os.chmod(path, int(metadata["mode"]))
                result["mode"] = True
            except (KeyError, OSError):
                pass
    except KeyError:
        pass
    try:
        os.chown(path, int(metadata["uid"]), int(metadata["gid"]), follow_symlinks=False)
        result["ownership"] = True
    except (KeyError, OSError, PermissionError, NotImplementedError):
        pass
    try:
        os.utime(path, ns=(int(metadata["atime_ns"]), int(metadata["mtime_ns"])), follow_symlinks=False)
        result["timestamps"] = True
    except (KeyError, OSError, NotImplementedError):
        pass
    return result


def _install_path(staged: Path, destination: Path, overwrite: bool) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if destination.exists() or destination.is_symlink():
        if not overwrite:
            raise ValueError(f"Destination already exists: {destination}")
        backup = destination.with_name(f".{destination.name}.portacode-old-{uuid.uuid4().hex}")
        os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except Exception:
        if backup is not None and backup.exists():
            os.replace(backup, destination)
        raise
    if backup is not None:
        if backup.is_dir() and not backup.is_symlink():
            shutil.rmtree(backup)
        else:
            backup.unlink(missing_ok=True)


def _finalize(message: Dict[str, Any]) -> Dict[str, Any]:
    manifest = _load_manifest(message.get("transfer_id"), message.get("authorization_token"))
    if manifest.get("role") != "destination":
        raise ValueError("Only a destination transfer can be finalized")
    if manifest.get("state") == "completed":
        return {"event": "transfer_finalized", **_public_manifest(manifest)}
    if len(set(manifest.get("completed_chunks", []))) != int(manifest["chunk_count"]):
        missing = sorted(set(range(int(manifest["chunk_count"]))) - set(manifest.get("completed_chunks", [])))
        raise ValueError(f"Transfer is incomplete; missing chunks: {missing[:20]}")
    partial = Path(manifest["partial_path"])
    actual_hash = _hash_file(partial)
    if not hmac.compare_digest(str(manifest.get("payload_hash") or ""), actual_hash):
        raise ValueError("Complete payload hash mismatch")
    destination = Path(manifest["destination_path"])
    metadata_result: Dict[str, bool] = {}
    if manifest["kind"] == "file":
        staged = Path(manifest.get("staged_path") or partial.with_name(f".portacode-staged-{manifest['transfer_id']}"))
        staged.unlink(missing_ok=True)
        os.replace(partial, staged)
        metadata_result = _apply_metadata(staged, manifest.get("metadata") or {})
        _install_path(staged, destination, bool(manifest.get("overwrite")))
    else:
        extraction_root = Path(manifest.get("extraction_path") or (
            _nearest_existing_parent(destination) / f".portacode-extract-{manifest['transfer_id']}"
        ))
        shutil.rmtree(extraction_root, ignore_errors=True)
        extraction_root.mkdir(mode=0o700)
        with tarfile.open(partial, "r:") as archive:
            members = list(_safe_members(archive, extraction_root))
            if len(members) != int(manifest.get("entry_count") or len(members)):
                raise ValueError("Archive entry count differs from the authorized manifest")
            archive.extractall(extraction_root, members=members, numeric_owner=True)
        roots = list(extraction_root.iterdir())
        if len(roots) != 1:
            raise ValueError("Folder archive must contain exactly one root item")
        _install_path(roots[0], destination, bool(manifest.get("overwrite")))
        shutil.rmtree(extraction_root, ignore_errors=True)
    manifest["state"] = "completed"
    manifest["completed_bytes"] = int(manifest["payload_size"])
    manifest["completed_at"] = time.time()
    manifest["metadata_preserved"] = metadata_result or {
        "mode": True,
        "ownership": os.geteuid() == 0 if hasattr(os, "geteuid") else False,
        "timestamps": True,
    }
    manifest["expires_at"] = time.time() + 3600
    _write_manifest(manifest)
    partial.unlink(missing_ok=True)
    return {"event": "transfer_finalized", **_public_manifest(manifest)}


class TransferPrepareHandler(SyncHandler):
    @property
    def command_name(self) -> str:
        return "transfer_prepare"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        _cleanup_expired()
        role = str(message.get("role") or "")
        result = _prepare_source(message) if role == "source" else _prepare_destination(message) if role == "destination" else None
        if result is None:
            raise ValueError("role must be source or destination")
        return {"event": "transfer_prepared", **result}


class TransferReadChunkHandler(SyncHandler):
    @property
    def command_name(self) -> str:
        return "transfer_read_chunk"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        return _read_chunk(message)


class TransferReceiveChunkHandler(SyncHandler):
    @property
    def command_name(self) -> str:
        return "transfer_receive_chunk"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        return _receive_chunk(message)


class TransferStatusHandler(SyncHandler):
    @property
    def command_name(self) -> str:
        return "transfer_status"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        manifest = _load_manifest(message.get("transfer_id"), message.get("authorization_token"))
        completed = set(int(value) for value in manifest.get("completed_chunks", []))
        missing = sorted(set(range(int(manifest["chunk_count"]))) - completed)
        return {"event": "transfer_status_response", **_public_manifest(manifest), "missing_chunks": missing}


class TransferFinalizeHandler(SyncHandler):
    @property
    def command_name(self) -> str:
        return "transfer_finalize"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        return _finalize(message)


class TransferCancelHandler(SyncHandler):
    @property
    def command_name(self) -> str:
        return "transfer_cancel"

    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        manifest = _load_manifest(message.get("transfer_id"), message.get("authorization_token"))
        transfer_id = manifest["transfer_id"]
        _cleanup_destination_staging(manifest)
        shutil.rmtree(_job_dir(transfer_id), ignore_errors=True)
        return {"event": "transfer_canceled", "transfer_id": transfer_id, "success": True}
