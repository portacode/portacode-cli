from __future__ import annotations

import os
import stat
import io
import tarfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from portacode.connection.handlers.resumable_transfer_handlers import (
    STORAGE_SAFETY_BYTES,
    TransferFinalizeHandler,
    TransferCancelHandler,
    TransferPrepareHandler,
    TransferReadChunkHandler,
    TransferReceiveChunkHandler,
    TransferStatusHandler,
)


TOKEN = "transfer-authorization-token-with-ample-entropy"


def _handler(handler_class):
    return handler_class(None, {})


def _copy_all_chunks(source_cache: Path, destination_cache: Path, prepared: dict, destination: Path):
    transfer_id = prepared["transfer_id"]
    common = {
        "transfer_id": transfer_id,
        "authorization_token": TOKEN,
    }
    os.environ["PORTACODE_TRANSFER_CACHE_DIR"] = str(destination_cache)
    destination_prepared = _handler(TransferPrepareHandler).execute({
        **common,
        "role": "destination",
        "kind": prepared["kind"],
        "path": str(destination),
        "payload_size": prepared["payload_size"],
        "payload_hash": prepared["payload_hash"],
        "chunk_size": prepared["chunk_size"],
        "chunk_count": prepared["chunk_count"],
        "expanded_size": prepared["expanded_size"],
        "expanded_storage_bytes": prepared["expanded_storage_bytes"],
        "entry_count": prepared["entry_count"],
        "metadata": prepared["metadata"],
        "archive_format": prepared["archive_format"],
    })
    assert destination_prepared["progress_percent"] == 0

    for index in range(prepared["chunk_count"]):
        os.environ["PORTACODE_TRANSFER_CACHE_DIR"] = str(source_cache)
        chunk = _handler(TransferReadChunkHandler).execute({**common, "chunk_index": index})
        os.environ["PORTACODE_TRANSFER_CACHE_DIR"] = str(destination_cache)
        received = _handler(TransferReceiveChunkHandler).execute({
            **common,
            "chunk_index": index,
            "chunk_hash": chunk["chunk_hash"],
            "content_base64": chunk["content_base64"],
        })
        expected = round(received["completed_bytes"] * 100 / max(received["payload_size"], 1), 2)
        assert received["progress_percent"] == expected

    # A newly constructed handler reads the persisted receipt map, simulating
    # both a WebSocket reconnect and a device-agent restart.
    status = _handler(TransferStatusHandler).execute(common)
    assert status["missing_chunks"] == []
    assert status["progress_percent"] == 100
    return _handler(TransferFinalizeHandler).execute(common)


def test_file_transfer_resumes_and_preserves_metadata(tmp_path, monkeypatch):
    source_cache = tmp_path / "source-cache"
    destination_cache = tmp_path / "destination-cache"
    source = tmp_path / "source.bin"
    source.write_bytes((b"portacode-transfer" * 20000) + b"tail")
    source.chmod(0o640)
    transfer_id = str(uuid.uuid4())
    monkeypatch.setenv("PORTACODE_TRANSFER_CACHE_DIR", str(source_cache))
    prepared = _handler(TransferPrepareHandler).execute({
        "role": "source",
        "kind": "file",
        "path": str(source),
        "transfer_id": transfer_id,
        "authorization_token": TOKEN,
        "chunk_size": 64 * 1024,
    })

    destination = tmp_path / "received.bin"
    finalized = _copy_all_chunks(source_cache, destination_cache, prepared, destination)

    assert destination.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert finalized["complete"] is True
    assert finalized["metadata_preserved"]["mode"] is True


def test_folder_transfer_resumes_and_preserves_tree_metadata(tmp_path, monkeypatch):
    source_cache = tmp_path / "source-cache"
    destination_cache = tmp_path / "destination-cache"
    source = tmp_path / "assets"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "photo.bin").write_bytes(b"image" * 30000)
    (source / "readme.txt").write_text("hello", encoding="utf-8")
    (source / "nested" / "photo.bin").chmod(0o600)
    transfer_id = str(uuid.uuid4())
    monkeypatch.setenv("PORTACODE_TRANSFER_CACHE_DIR", str(source_cache))
    prepared = _handler(TransferPrepareHandler).execute({
        "role": "source",
        "kind": "folder",
        "path": str(source),
        "transfer_id": transfer_id,
        "authorization_token": TOKEN,
        "chunk_size": 64 * 1024,
    })

    destination = tmp_path / "copied-assets"
    finalized = _copy_all_chunks(source_cache, destination_cache, prepared, destination)

    assert (destination / "readme.txt").read_text(encoding="utf-8") == "hello"
    assert (destination / "nested" / "photo.bin").read_bytes() == b"image" * 30000
    assert stat.S_IMODE((destination / "nested" / "photo.bin").stat().st_mode) == 0o600
    assert finalized["kind"] == "folder"
    assert finalized["metadata_preserved"]["mode"] is True


def test_transfer_token_is_bound_and_required(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTACODE_TRANSFER_CACHE_DIR", str(tmp_path / "cache"))
    source = tmp_path / "source.txt"
    source.write_text("secret", encoding="utf-8")
    transfer_id = str(uuid.uuid4())
    _handler(TransferPrepareHandler).execute({
        "role": "source",
        "kind": "file",
        "path": str(source),
        "transfer_id": transfer_id,
        "authorization_token": TOKEN,
    })

    with pytest.raises(PermissionError, match="authorization is invalid"):
        _handler(TransferStatusHandler).execute({
            "transfer_id": transfer_id,
            "authorization_token": "a-different-token-with-enough-characters",
        })


def test_destination_reports_peak_storage_deficit(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTACODE_TRANSFER_CACHE_DIR", str(tmp_path / "cache"))
    transfer_id = str(uuid.uuid4())
    payload_size = 20 * 1024 * 1024
    expanded = 80 * 1024 * 1024
    available = 10 * 1024 * 1024
    with patch(
        "portacode.connection.handlers.resumable_transfer_handlers._free_bytes",
        return_value=available,
    ), patch(
        "portacode.connection.handlers.resumable_transfer_handlers._filesystem_block_size",
        return_value=4096,
    ), pytest.raises(ValueError) as error:
        _handler(TransferPrepareHandler).execute({
            "role": "destination",
            "kind": "folder",
            "path": str(tmp_path / "destination"),
            "transfer_id": transfer_id,
            "authorization_token": TOKEN,
            "payload_size": payload_size,
            "payload_hash": "a" * 64,
            "expanded_storage_bytes": expanded,
            "chunk_size": 64 * 1024,
            "chunk_count": payload_size // (64 * 1024),
        })
    entry_overhead = 4096
    required = payload_size + expanded + entry_overhead + STORAGE_SAFETY_BYTES
    message = str(error.value)
    assert f"required {required} bytes" in message
    assert f"available {available} bytes" in message
    assert f"short by {required - available} bytes" in message


def test_folder_finalize_rejects_symlink_chain_archive(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setenv("PORTACODE_TRANSFER_CACHE_DIR", str(cache))
    transfer_id = str(uuid.uuid4())
    payload = tmp_path / "hostile.tar"
    with tarfile.open(payload, "w") as archive:
        root = tarfile.TarInfo("root/")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        link = tarfile.TarInfo("root/pivot")
        link.type = tarfile.SYMTYPE
        link.linkname = "."
        archive.addfile(link)
        content = b"must not extract"
        nested = tarfile.TarInfo("root/pivot/file.txt")
        nested.size = len(content)
        archive.addfile(nested, io.BytesIO(content))

    digest = __import__("hashlib").sha256(payload.read_bytes()).hexdigest()
    common = {"transfer_id": transfer_id, "authorization_token": TOKEN}
    prepared = _handler(TransferPrepareHandler).execute({
        **common, "role": "destination", "kind": "folder",
        "path": str(tmp_path / "output"), "payload_size": payload.stat().st_size,
        "payload_hash": digest, "chunk_size": 64 * 1024,
        "chunk_count": 1, "expanded_storage_bytes": len(content), "entry_count": 3,
    })
    received = _handler(TransferReceiveChunkHandler).execute({
        **common, "chunk_index": 0, "chunk_hash": digest,
        "content_base64": __import__("base64").b64encode(payload.read_bytes()).decode(),
    })
    assert received["progress_percent"] == 100
    with pytest.raises(ValueError, match="Archive links are forbidden"):
        _handler(TransferFinalizeHandler).execute(common)
    assert not (tmp_path / "output").exists()


def test_destination_stages_on_destination_filesystem_and_cancel_cleans_it(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    destination_root = tmp_path / "destination-filesystem"
    destination_root.mkdir()
    destination = destination_root / "nested" / "received.bin"
    monkeypatch.setenv("PORTACODE_TRANSFER_CACHE_DIR", str(cache))
    transfer_id = str(uuid.uuid4())
    common = {"transfer_id": transfer_id, "authorization_token": TOKEN}
    _handler(TransferPrepareHandler).execute({
        **common, "role": "destination", "kind": "file", "path": str(destination),
        "payload_size": 4, "payload_hash": "0" * 64, "chunk_size": 32768,
        "chunk_count": 1, "expanded_storage_bytes": 4, "entry_count": 1,
    })
    manifest = __import__("json").loads((cache / transfer_id / "manifest.json").read_text())
    partial = Path(manifest["partial_path"])
    assert partial.parent == destination_root
    assert partial.exists()
    _handler(TransferCancelHandler).execute(common)
    assert not partial.exists()
    assert not (cache / transfer_id).exists()


def test_folder_source_rejects_links_before_archiving(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "real.txt").write_text("data", encoding="utf-8")
    (source / "link.txt").symlink_to("real.txt")
    monkeypatch.setenv("PORTACODE_TRANSFER_CACHE_DIR", str(tmp_path / "cache"))
    with pytest.raises(ValueError, match="do not support symbolic links"):
        _handler(TransferPrepareHandler).execute({
            "role": "source", "kind": "folder", "path": str(source),
            "transfer_id": str(uuid.uuid4()), "authorization_token": TOKEN,
        })
