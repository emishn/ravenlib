import argparse
import hashlib
import ipaddress
import json
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

try:
    from .gui_client import (
        DEFAULT_SERVER_URL,
        MANIFESTS_DIR,
        ClientConfig,
        format_bytes,
        LocalManifestStore,
        ManifestDiffer,
        ProjectManifestBuilder,
        resource_base_dir,
        SyncClientError,
        SyncApiClient,
        writable_app_dir,
    )
    from .app_info import APP_VERSION, WEB_CLIENT_SERVER_VERSION
except ImportError:
    from gui_client import (
        DEFAULT_SERVER_URL,
        MANIFESTS_DIR,
        ClientConfig,
        format_bytes,
        LocalManifestStore,
        ManifestDiffer,
        ProjectManifestBuilder,
        resource_base_dir,
        SyncClientError,
        SyncApiClient,
        writable_app_dir,
    )
    from app_info import APP_VERSION, WEB_CLIENT_SERVER_VERSION


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ravenlib.web_client")

BASE_DIR = resource_base_dir()
WEBUI_DIR = BASE_DIR / "webui"
MAX_OPERATION_LOG_ENTRIES: Final[int] = 1000
MAX_STORED_OPERATIONS: Final[int] = 32
COMPLETED_OPERATION_TTL_SECONDS: Final[int] = 1800
FINAL_OPERATION_STATES: Final[frozenset[str]] = frozenset({"completed", "failed"})
STATIC_FILES: Final[tuple[str, ...]] = ("index.html", "styles.css", "app.js")
UI_SESSION_STALE_SECONDS: Final[int] = 20
UI_SHUTDOWN_GRACE_SECONDS: Final[float] = 2.0
LOCAL_BIND_HOST: Final[str] = "127.0.0.1"


def normalize_local_bind_host(host: str) -> str:
    """Return the loopback bind address and reject any network-facing address."""

    candidate = str(host).strip()
    if candidate.lower() == "localhost":
        return LOCAL_BIND_HOST

    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError(
            "The local web client accepts only a loopback host: 127.0.0.1 or localhost."
        ) from exc

    if not address.is_loopback:
        raise ValueError(
            "The local web client accepts only a loopback host: 127.0.0.1 or localhost."
        )
    return LOCAL_BIND_HOST


def build_asset_token() -> str:
    digest = hashlib.sha256()
    for filename in STATIC_FILES:
        path = WEBUI_DIR / filename
        try:
            stat = path.stat()
        except OSError:
            digest.update(f"{filename}:missing".encode("utf-8"))
            continue
        digest.update(f"{filename}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8"))
    return digest.hexdigest()[:12]


ASSET_TOKEN = build_asset_token()


def _build_ico_from_png_bytes(png_bytes: bytes) -> bytes:
    if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Window icon source must be a PNG file.")

    width = height = 0
    offset = 8
    while offset + 8 <= len(png_bytes):
        chunk_length = int.from_bytes(png_bytes[offset:offset + 4], "big")
        chunk_type = png_bytes[offset + 4:offset + 8]
        chunk_data_start = offset + 8
        chunk_data_end = chunk_data_start + chunk_length
        if chunk_data_end + 4 > len(png_bytes):
            break
        if chunk_type == b"IHDR" and chunk_length >= 8:
            width = int.from_bytes(png_bytes[chunk_data_start:chunk_data_start + 4], "big")
            height = int.from_bytes(png_bytes[chunk_data_start + 4:chunk_data_start + 8], "big")
            break
        offset = chunk_data_end + 4

    if width <= 0 or height <= 0:
        raise ValueError("Window icon PNG is missing a valid IHDR chunk.")

    icon_width = 0 if width >= 256 else width
    icon_height = 0 if height >= 256 else height
    header_size = 6 + 16
    directory_entry = (
        bytes((icon_width, icon_height, 0, 0))
        + (1).to_bytes(2, "little")
        + (32).to_bytes(2, "little")
        + len(png_bytes).to_bytes(4, "little")
        + header_size.to_bytes(4, "little")
    )
    return (
        (0).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + directory_entry
        + png_bytes
    )


def ensure_window_icon_path() -> Path | None:
    png_path = WEBUI_DIR / "ravenfall_code.png"
    if not png_path.exists():
        logger.warning("Window icon source not found: %s", png_path)
        return None

    ico_path = writable_app_dir() / "ravenfall_code.ico"
    try:
        source_stat = png_path.stat()
        target_stat = ico_path.stat() if ico_path.exists() else None
        if target_stat and target_stat.st_mtime_ns >= source_stat.st_mtime_ns and target_stat.st_size > 0:
            return ico_path

        ico_bytes = _build_ico_from_png_bytes(png_path.read_bytes())
        ico_path.write_bytes(ico_bytes)
        return ico_path
    except OSError as exc:
        logger.warning("Failed to prepare window icon file: %s", exc)
        return None
    except ValueError as exc:
        logger.warning("Failed to prepare window icon file: %s", exc)
        return None


class EmbeddedWindowApi:
    def __init__(self) -> None:
        self._window = None

    def attach_window(self, window) -> None:
        self._window = window

    def close_window(self, _reason: str = "manual-end-session") -> dict:
        if self._window is not None:
            self._window.destroy()
        return {"closed": True}


@dataclass
class OperationState:
    operation_id: str
    operation_type: str
    project_name: str
    state: str = "running"
    status_text: str = "Starting..."
    progress_current: int = 0
    progress_total: int = 1
    logs: deque[dict] = field(default_factory=lambda: deque(maxlen=MAX_OPERATION_LOG_ENTRIES))
    log_total: int = 0
    result: dict | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def append_log(self, message: str, level: str = "info") -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_total += 1
        self.logs.append({"timestamp": timestamp, "level": level, "message": message})
        self.updated_at = time.time()

    def set_progress(self, text: str, current: int, total: int) -> None:
        self.status_text = text
        self.progress_current = max(0, current)
        self.progress_total = max(1, total or 1)
        self.updated_at = time.time()

    def as_dict(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "operation_type": self.operation_type,
            "project_name": self.project_name,
            "state": self.state,
            "status_text": self.status_text,
            "progress_current": self.progress_current,
            "progress_total": self.progress_total,
            "logs": list(self.logs),
            "log_total": self.log_total,
            "log_limit": MAX_OPERATION_LOG_ENTRIES,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class RavenLibWebService:
    def __init__(self) -> None:
        self.config_store = ClientConfig()
        self.manifest_builder = ProjectManifestBuilder()
        self.api_client = SyncApiClient(self.config_store)
        self.local_manifest_store = LocalManifestStore()
        self.manifest_differ = ManifestDiffer()
        self._operations: dict[str, OperationState] = {}
        self._ui_sessions: dict[str, float] = {}
        self._ui_session_generation = 0
        self._auto_shutdown_on_last_session_close = False
        self._server: ThreadingHTTPServer | None = None
        self._shutdown_requested = False
        self._lock = threading.Lock()

    def attach_server(self, server: ThreadingHTTPServer, *, auto_shutdown_on_last_session_close: bool) -> None:
        with self._lock:
            self._server = server
            self._auto_shutdown_on_last_session_close = auto_shutdown_on_last_session_close
            self._shutdown_requested = False
            self._ui_sessions.clear()
            self._ui_session_generation += 1

    def get_config(self) -> dict:
        config = self.config_store.load()
        return {
            "server_url": config.get("server_url", DEFAULT_SERVER_URL),
            "project_name": config.get("project_name", ""),
            "project_dir": config.get("project_dir", ""),
            "commit_message": config.get("commit_message", ""),
            "theme": config.get("theme", "dark"),
        }

    def save_config(self, payload: dict) -> dict:
        config = {
            "server_url": str(payload.get("server_url", DEFAULT_SERVER_URL)).strip() or DEFAULT_SERVER_URL,
            "project_name": str(payload.get("project_name", "")).strip(),
            "project_dir": str(payload.get("project_dir", "")).strip(),
            "commit_message": str(payload.get("commit_message", "")).strip(),
            "theme": "light" if str(payload.get("theme", "dark")).strip().lower() == "light" else "dark",
        }
        self.config_store.save(config)
        return config

    def get_operation(self, operation_id: str) -> dict:
        with self._lock:
            self._prune_operations_locked()
            operation = self._operations.get(operation_id)
            if operation is None:
                raise KeyError(operation_id)
            return operation.as_dict()

    def request_connection_status(self, server_url: str) -> dict:
        return self.api_client.fetch_connection_status(server_url.strip() or DEFAULT_SERVER_URL)

    def open_ui_session(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            raise ValueError("Session ID is required.")

        now = time.time()
        with self._lock:
            self._prune_ui_sessions_locked(now)
            self._ui_sessions[session_id] = now
            self._ui_session_generation += 1
        return {"session_id": session_id, "active_sessions": self._active_ui_session_count()}

    def touch_ui_session(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            raise ValueError("Session ID is required.")

        now = time.time()
        with self._lock:
            self._prune_ui_sessions_locked(now)
            self._ui_sessions[session_id] = now
        return {"session_id": session_id, "active_sessions": self._active_ui_session_count()}

    def close_ui_session(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            raise ValueError("Session ID is required.")

        with self._lock:
            self._ui_sessions.pop(session_id, None)
            self._ui_session_generation += 1
            self._prune_ui_sessions_locked()
            self._maybe_schedule_shutdown_check_locked("ui-session-closed")
            active_sessions = len(self._ui_sessions)
        return {"closed": True, "active_sessions": active_sessions}

    def shutdown_client(self, reason: str = "manual") -> dict:
        with self._lock:
            self._request_shutdown_locked(reason)
        return {"accepted": True, "message": "RavenLib web client is shutting down."}

    def start_sync(self, payload: dict) -> dict:
        project_name, project_dir, server_url, commit_message = self._validate_common_payload(payload)
        self.save_config(
            {
                "server_url": server_url,
                "project_name": project_name,
                "project_dir": str(project_dir),
                "commit_message": commit_message,
            }
        )
        operation = self._create_operation("sync", project_name)
        thread = threading.Thread(
            target=self._run_sync,
            args=(operation.operation_id, project_name, project_dir, server_url, commit_message),
            daemon=True,
        )
        thread.start()
        return operation.as_dict()

    def start_pull(self, payload: dict) -> dict:
        project_name, project_dir, server_url, _commit_message = self._validate_common_payload(payload)
        self.save_config(
            {
                "server_url": server_url,
                "project_name": project_name,
                "project_dir": str(project_dir),
                "commit_message": str(payload.get("commit_message", "")).strip(),
            }
        )
        operation = self._create_operation("pull", project_name)
        thread = threading.Thread(
            target=self._run_pull,
            args=(operation.operation_id, project_name, project_dir, server_url),
            daemon=True,
        )
        thread.start()
        return operation.as_dict()

    def _validate_common_payload(self, payload: dict) -> tuple[str, Path, str, str]:
        project_name = str(payload.get("project_name", "")).strip()
        project_dir_raw = str(payload.get("project_dir", "")).strip()
        server_url = str(payload.get("server_url", DEFAULT_SERVER_URL)).strip() or DEFAULT_SERVER_URL
        commit_message = str(payload.get("commit_message", "")).strip()

        if not project_name:
            raise ValueError("Project name is required.")
        if not project_dir_raw:
            raise ValueError("Project folder is required.")

        project_dir = Path(project_dir_raw)
        if not project_dir.is_dir():
            raise ValueError("Project folder does not exist.")

        return project_name, project_dir, server_url, commit_message

    def _create_operation(self, operation_type: str, project_name: str) -> OperationState:
        operation = OperationState(
            operation_id=uuid.uuid4().hex,
            operation_type=operation_type,
            project_name=project_name,
        )
        operation.append_log(f"{operation_type.capitalize()} started for '{project_name}'.")
        with self._lock:
            self._operations[operation.operation_id] = operation
            self._prune_operations_locked()
        return operation

    def _prune_operations_locked(self) -> None:
        now = time.time()
        expired_operation_ids = [
            operation_id
            for operation_id, operation in self._operations.items()
            if operation.state in FINAL_OPERATION_STATES
            and now - operation.updated_at > COMPLETED_OPERATION_TTL_SECONDS
        ]
        for operation_id in expired_operation_ids:
            self._operations.pop(operation_id, None)

        if len(self._operations) <= MAX_STORED_OPERATIONS:
            return

        finished_operations = sorted(
            (
                (operation.updated_at, operation_id)
                for operation_id, operation in self._operations.items()
                if operation.state in FINAL_OPERATION_STATES
            ),
            key=lambda item: item[0],
        )
        while len(self._operations) > MAX_STORED_OPERATIONS and finished_operations:
            _updated_at, operation_id = finished_operations.pop(0)
            self._operations.pop(operation_id, None)

    def _active_ui_session_count(self) -> int:
        with self._lock:
            self._prune_ui_sessions_locked()
            return len(self._ui_sessions)

    def _prune_ui_sessions_locked(self, now: float | None = None) -> None:
        current_time = now if now is not None else time.time()
        stale_session_ids = [
            session_id
            for session_id, last_seen in self._ui_sessions.items()
            if current_time - last_seen > UI_SESSION_STALE_SECONDS
        ]
        for session_id in stale_session_ids:
            self._ui_sessions.pop(session_id, None)

    def _maybe_schedule_shutdown_check_locked(self, reason: str) -> None:
        if not self._auto_shutdown_on_last_session_close or self._shutdown_requested or self._server is None:
            return

        if self._ui_sessions:
            return

        generation = self._ui_session_generation
        server = self._server

        def delayed_shutdown_check() -> None:
            time.sleep(UI_SHUTDOWN_GRACE_SECONDS)
            with self._lock:
                self._prune_ui_sessions_locked()
                if self._ui_session_generation != generation or self._ui_sessions:
                    return
                self._request_shutdown_locked(reason)

        threading.Thread(target=delayed_shutdown_check, daemon=True).start()
        logger.info("Scheduled web client shutdown check: reason=%s grace=%ss", reason, UI_SHUTDOWN_GRACE_SECONDS)

    def _request_shutdown_locked(self, reason: str) -> None:
        if self._shutdown_requested or self._server is None:
            return

        self._shutdown_requested = True
        server = self._server

        def shutdown_server() -> None:
            logger.info("Stopping RavenLib web client server: %s", reason)
            server.shutdown()

        threading.Thread(target=shutdown_server, daemon=True).start()

    def _with_operation(self, operation_id: str, callback: Callable[[OperationState], None]) -> None:
        logger.info("[LOCK-AUDIT] waiting for operation lock | operation_id=%s", operation_id)
        with self._lock:
            logger.info("[LOCK-AUDIT] acquired operation lock | operation_id=%s", operation_id)
            try:
                operation = self._operations[operation_id]
                callback(operation)
            finally:
                logger.info("[LOCK-AUDIT] releasing operation lock | operation_id=%s", operation_id)

    def _append_log(self, operation_id: str, message: str, level: str = "info") -> None:
        self._with_operation(operation_id, lambda operation: operation.append_log(message, level))

    def _set_progress(self, operation_id: str, text: str, current: int, total: int) -> None:
        logger.info(
            "[PROGRESS-AUDIT] applying progress update | operation_id=%s message=%s current=%s total=%s",
            operation_id,
            text,
            current,
            total,
        )
        self._with_operation(operation_id, lambda operation: operation.set_progress(text, current, total))
        logger.info(
            "[PROGRESS-AUDIT] progress updated | operation_id=%s message=%s current=%s total=%s",
            operation_id,
            text,
            current,
            total,
        )

    def _complete_operation(self, operation_id: str, result: dict, status_text: str) -> None:
        def mutate(operation: OperationState) -> None:
            operation.state = "completed"
            operation.result = result
            operation.error = None
            operation.set_progress(status_text, operation.progress_total, operation.progress_total)
            operation.append_log(status_text, "success")

        self._with_operation(operation_id, mutate)

    def _fail_operation(self, operation_id: str, error_message: str) -> None:
        def mutate(operation: OperationState) -> None:
            operation.state = "failed"
            operation.error = error_message
            operation.pending_sync = None
            operation.set_progress("Failed", 0, 1)
            operation.append_log(f"Operation failed: {error_message}", "error")

        self._with_operation(operation_id, mutate)

    def _run_sync(
        self,
        operation_id: str,
        project_name: str,
        project_dir: Path,
        server_url: str,
        commit_message: str,
    ) -> None:
        try:
            self._set_progress(operation_id, "Collecting files for scan...", 0, 1)
            self._append_log(operation_id, f"Scanning folder: {project_dir}")

            def on_scan_progress(current: int, total: int, relative_path: str) -> None:
                self._set_progress(operation_id, f"Scanning files: {current}/{total} ({relative_path})", current, total)
                if current == 1 or current == total or current % 25 == 0:
                    self._append_log(operation_id, f"Scanned {current}/{total}: {relative_path}")

            manifest = self.manifest_builder.build(
                project_name,
                project_dir,
                commit_message=commit_message,
                progress_callback=on_scan_progress,
            )
            self._append_log(operation_id, f"Scan completed: {len(manifest['files'])} file(s).")

            previous_manifest = self.local_manifest_store.load(project_dir)
            diff = self.manifest_differ.diff(previous_manifest, manifest)
            self._append_log(
                operation_id,
                f"Local diff: +{len(diff['added'])} ~{len(diff['modified'])} -{len(diff['removed'])}, "
                f"unchanged {diff['unchanged']}.",
            )

            self._set_progress(operation_id, "Checking latest server manifest...", 0, 1)
            self._append_log(operation_id, "Checking current server state.")
            server_latest = self.api_client.fetch_latest_manifest(server_url, project_name)
            if self._has_conflict(previous_manifest, server_latest):
                self._fail_operation(
                    operation_id,
                    "Server contains newer changes. Use Pull Latest before syncing.",
                )
                return

            self._run_sync_upload(operation_id, project_dir, server_url, manifest, diff)
        except (SyncClientError, OSError, json.JSONDecodeError, ValueError) as exc:
            logger.exception("Sync failed")
            self._fail_operation(operation_id, str(exc))

    def _run_sync_upload(
        self,
        operation_id: str,
        project_dir: Path,
        server_url: str,
        manifest: dict,
        diff: dict
    ) -> None:
        try:

            MANIFESTS_DIR.mkdir(exist_ok=True)
            snapshot_path = MANIFESTS_DIR / f"{manifest['project']}_{int(time.time())}.json"
            snapshot_path.write_text(json.dumps(manifest, indent=4, ensure_ascii=False), encoding="utf-8")
            self._append_log(operation_id, f"Saved local manifest snapshot: {snapshot_path.name}")

            self._set_progress(operation_id, "Sending manifest to server...", 0, 1)
            self._append_log(operation_id, f"Sending manifest to {server_url}.")
            response = self.api_client.send_manifest(server_url, manifest)
            sync_id = response["sync_id"]
            self._append_log(
                operation_id,
                f"Server staged manifest. Missing objects: {len(response.get('need_upload', []))}. "
                f"Sync ID: {sync_id}",
            )

            uploaded = self._upload_missing_objects(
                operation_id,
                project_dir,
                manifest,
                server_url,
                response.get("need_upload", []),
                sync_id,
            )

            self._set_progress(operation_id, "Finalizing sync...", 0, 1)
            self._append_log(operation_id, "Finalizing sync on server.")
            finalize_response = self.api_client.finalize_sync(server_url, sync_id)
            self._append_log(operation_id, "Server finalized sync successfully.", "success")

            self.local_manifest_store.save(project_dir, manifest)
            self._append_log(operation_id, "Local sync state updated.", "success")
            result = {
                "project": manifest["project"],
                "commit_message": manifest.get("commit_message", ""),
                "files": len(manifest["files"]),
                "accepted": finalize_response.get("accepted"),
                "message": finalize_response.get("message", ""),
                "received_at": finalize_response.get("received_at", ""),
                "sender": finalize_response.get("sender", ""),
                "manifest_file": finalize_response.get("manifest_file", ""),
                "server_commit_message": finalize_response.get("commit_message", ""),
                "need_upload": response.get("need_upload", []),
                "diff": diff,
                "uploaded": uploaded,
                "object_count": finalize_response.get("object_count", 0),
                "sync_id": finalize_response.get("sync_id") or response.get("sync_id", ""),
            }
            self._complete_operation(operation_id, result, "Sync complete.")
        except (SyncClientError, OSError, json.JSONDecodeError, ValueError) as exc:
            logger.exception("Sync failed")
            self._fail_operation(operation_id, str(exc))

    def _run_pull(self, operation_id: str, project_name: str, project_dir: Path, server_url: str) -> None:
        try:
            self._set_progress(operation_id, "Fetching latest manifest from server...", 0, 1)
            server_latest = self.api_client.fetch_latest_manifest(server_url, project_name)
            if server_latest is None:
                raise ValueError("Server has no manifest for this project yet.")

            self._append_log(operation_id, "Latest manifest downloaded. Applying to local files.")
            result = self._apply_manifest_to_disk(operation_id, project_name, project_dir, server_url, server_latest)
            self.local_manifest_store.save(
                project_dir,
                {
                    "project": project_name,
                    "commit_message": server_latest.get("commit_message", ""),
                    "files": server_latest["files"],
                },
            )
            self._complete_operation(operation_id, result, "Pull complete.")
        except (SyncClientError, OSError, json.JSONDecodeError, ValueError) as exc:
            logger.exception("Pull failed")
            self._fail_operation(operation_id, str(exc))

    def _upload_missing_objects(
        self,
        operation_id: str,
        project_dir: Path,
        manifest: dict,
        server_url: str,
        need_upload: list[str],
        sync_id: str,
    ) -> int:
        hash_to_path = {}
        hash_to_size = {}
        for file_entry in manifest["files"]:
            hash_to_path.setdefault(file_entry["sha256"], file_entry["path"])
            hash_to_size.setdefault(file_entry["sha256"], int(file_entry.get("size", 0)))

        total_uploads = len(need_upload)
        total_upload_bytes = sum(hash_to_size.get(sha256, 0) for sha256 in need_upload)
        uploaded_bytes = 0
        uploaded = 0
        if total_uploads == 0:
            self._set_progress(operation_id, "Upload skipped: all objects already exist on server.", 1, 1)
            self._append_log(operation_id, "Upload skipped: server already has all required objects.", "success")
            return uploaded

        def emit_upload_audit(stage: str, relative_path: str, index: int, **details: object) -> None:
            detail_text = " ".join(f"{key}={value}" for key, value in details.items())
            message = (
                f"[UPLOAD-AUDIT] {stage} | item={index}/{total_uploads} path={relative_path}"
                f"{(' ' + detail_text) if detail_text else ''}"
            )
            logger.info(message)
            self._append_log(operation_id, message)

        self._append_log(operation_id, f"Uploading {total_uploads} object(s).")
        for index, sha256 in enumerate(need_upload, start=1):
            relative_path = hash_to_path.get(sha256)
            if relative_path is None:
                self._set_progress(
                    operation_id,
                    f"Uploading objects: {index}/{total_uploads} (missing local file)",
                    index,
                    total_uploads,
                )
                message = f"Missing local file for object {sha256}."
                self._append_log(operation_id, message, "error")
                raise SyncClientError(message)

            try:
                emit_upload_audit("next file dequeued", relative_path, index, sha256=sha256)
                file_path = project_dir / relative_path
                file_size = hash_to_size.get(sha256, file_path.stat().st_size)
                self._set_progress(
                    operation_id,
                    f"Uploading objects: {index}/{total_uploads} ({relative_path})",
                    uploaded_bytes,
                    total_upload_bytes or total_uploads,
                )
                self._append_log(operation_id, f"Uploading {index}/{total_uploads}: {relative_path}")
                emit_upload_audit("next upload started", relative_path, index, file_size=file_size)

                def on_upload_progress(current_bytes: int, total_bytes: int) -> None:
                    total = total_upload_bytes or total_bytes or total_uploads
                    current = uploaded_bytes + current_bytes if total_upload_bytes else index - 1
                    self._set_progress(
                        operation_id,
                        (
                            f"Uploading objects: {index}/{total_uploads} ({relative_path}) "
                            f"{format_bytes(current_bytes)} / {format_bytes(total_bytes)}"
                        ),
                        current,
                        total,
                    )

                self.api_client.upload_object_file(
                    server_url,
                    sha256,
                    file_path,
                    sync_id=sync_id,
                    progress_callback=on_upload_progress,
                    log_callback=lambda message: self._append_log(operation_id, message),
                )
                emit_upload_audit(
                    "future completed",
                    relative_path,
                    index,
                    note="not-used; sequential upload call returned",
                )
                emit_upload_audit(
                    "queue task_done()",
                    relative_path,
                    index,
                    note="not-used; no upload queue in active path",
                )
                emit_upload_audit(
                    "semaphore released",
                    relative_path,
                    index,
                    note="not-used; no semaphore in active path",
                )
                uploaded += 1
                uploaded_bytes += file_size
                self._set_progress(
                    operation_id,
                    f"Uploading objects: {index}/{total_uploads} ({relative_path})",
                    uploaded_bytes if total_upload_bytes else index,
                    total_upload_bytes or total_uploads,
                )
                emit_upload_audit(
                    "progress updated",
                    relative_path,
                    index,
                    current=uploaded_bytes if total_upload_bytes else index,
                    total=total_upload_bytes or total_uploads,
                )
            except (SyncClientError, OSError) as exc:
                self._set_progress(
                    operation_id,
                    f"Uploading objects: {index}/{total_uploads} ({relative_path})",
                    uploaded_bytes if total_upload_bytes else index,
                    total_upload_bytes or total_uploads,
                )
                message = f"Upload failed for {relative_path}: {exc}"
                self._append_log(operation_id, message, "error")
                raise SyncClientError(message) from exc

        self._append_log(operation_id, f"Upload finished: {uploaded} succeeded.", "success")
        return uploaded

    def _apply_manifest_to_disk(
        self,
        operation_id: str,
        project_name: str,
        project_dir: Path,
        server_url: str,
        target_manifest: dict,
    ) -> dict:
        def on_scan_progress(current: int, total: int, relative_path: str) -> None:
            self._set_progress(operation_id, f"Scanning local files: {current}/{total} ({relative_path})", current, total)
            if current == 1 or current == total or current % 25 == 0:
                self._append_log(operation_id, f"Scanned local file {current}/{total}: {relative_path}")

        current_scan = self.manifest_builder.build(project_name, project_dir, progress_callback=on_scan_progress)
        current_files = {f["path"]: f["sha256"] for f in current_scan["files"]}
        target_files = {
            f["path"]: {"sha256": f["sha256"], "size": int(f.get("size", 0))}
            for f in target_manifest["files"]
        }

        applied = 0
        files_to_apply = [
            (relative_path, entry["sha256"], entry["size"])
            for relative_path, entry in target_files.items()
            if current_files.get(relative_path) != entry["sha256"]
        ]
        total_apply = len(files_to_apply)
        total_apply_bytes = sum(size for _relative_path, _sha256, size in files_to_apply)
        applied_bytes = 0
        for index, (relative_path, sha256, expected_size) in enumerate(files_to_apply, start=1):
            self._set_progress(
                operation_id,
                f"Applying files: {index}/{total_apply} ({relative_path})",
                applied_bytes,
                total_apply_bytes or total_apply,
            )
            self._append_log(operation_id, f"Downloading {index}/{total_apply}: {relative_path}")
            destination = project_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)

            def on_download_progress(current_bytes: int, total_bytes: int) -> None:
                total = total_apply_bytes or total_bytes or total_apply
                current = applied_bytes + current_bytes if total_apply_bytes else index - 1
                self._set_progress(
                    operation_id,
                    (
                        f"Applying files: {index}/{total_apply} ({relative_path}) "
                        f"{format_bytes(current_bytes)} / {format_bytes(total_bytes)}"
                    ),
                    current,
                    total,
                )

            download_info = self.api_client.download_object_to_path(
                server_url,
                sha256,
                destination,
                progress_callback=on_download_progress,
                log_callback=lambda message: self._append_log(operation_id, message),
            )
            applied += 1
            applied_bytes += max(expected_size, int(download_info.get("size", 0)))
            self._set_progress(
                operation_id,
                f"Applying files: {index}/{total_apply} ({relative_path})",
                applied_bytes if total_apply_bytes else index,
                total_apply_bytes or total_apply,
            )

        removed = 0
        files_to_remove = [relative_path for relative_path in current_files if relative_path not in target_files]
        total_remove = len(files_to_remove)
        for index, relative_path in enumerate(files_to_remove, start=1):
            self._set_progress(operation_id, f"Removing files: {index}/{total_remove} ({relative_path})", index - 1, total_remove)
            self._append_log(operation_id, f"Removing {index}/{total_remove}: {relative_path}")
            (project_dir / relative_path).unlink(missing_ok=True)
            removed += 1
            self._set_progress(operation_id, f"Removing files: {index}/{total_remove} ({relative_path})", index, total_remove)

        self._append_log(operation_id, f"Apply finished: {applied} updated, {removed} removed.")
        return {
            "project": project_name,
            "applied": applied,
            "removed": removed,
            "server_commit_message": target_manifest.get("commit_message", ""),
        }



SERVICE = RavenLibWebService()


class RavenLibWebHandler(BaseHTTPRequestHandler):
    server_version = WEB_CLIENT_SERVER_VERSION

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if path == "/":
                return self._serve_static("index.html", "text/html; charset=utf-8")
            if path == "/favicon.ico":
                return self._serve_static("ravenfall_code.png", "image/png")
            if path == "/ravenfall_code.png":
                return self._serve_static("ravenfall_code.png", "image/png")
            if path == "/styles.css":
                return self._serve_static("styles.css", "text/css; charset=utf-8")
            if path == "/app.js":
                return self._serve_static("app.js", "application/javascript; charset=utf-8")
            if path == "/api/config":
                return self._send_json(HTTPStatus.OK, SERVICE.get_config())
            if path == "/api/connection-status":
                server_url = query.get("server_url", [DEFAULT_SERVER_URL])[0]
                return self._send_json(
                    HTTPStatus.OK,
                    SERVICE.request_connection_status(server_url),
                )
            if path.startswith("/api/operations/"):
                operation_id = path.rsplit("/", 1)[-1]
                return self._send_json(HTTPStatus.OK, SERVICE.get_operation(operation_id))
        except KeyError:
            return self._send_error_json(HTTPStatus.NOT_FOUND, "Operation not found.")
        except ValueError as exc:
            return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except SyncClientError as exc:
            logger.warning("GET bridge request failed: path=%s error=%s", path, exc)
            return self._send_error_json(HTTPStatus.BAD_GATEWAY, str(exc))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            logger.warning("GET bridge request failed: path=%s error=%s", path, exc)
            return self._send_error_json(HTTPStatus.BAD_GATEWAY, str(exc))
        except Exception as exc:
            logger.exception("Unhandled GET bridge failure: path=%s", path)
            return self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        self._send_error_json(HTTPStatus.NOT_FOUND, "Route not found.")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            payload = self._read_json_body()
            if path == "/api/config":
                return self._send_json(HTTPStatus.OK, SERVICE.save_config(payload))
            if path == "/api/sync":
                return self._send_json(HTTPStatus.ACCEPTED, SERVICE.start_sync(payload))
            if path == "/api/pull":
                return self._send_json(HTTPStatus.ACCEPTED, SERVICE.start_pull(payload))
            if path == "/api/ui-session/open":
                return self._send_json(HTTPStatus.OK, SERVICE.open_ui_session(payload))
            if path == "/api/ui-session/ping":
                return self._send_json(HTTPStatus.OK, SERVICE.touch_ui_session(payload))
            if path == "/api/ui-session/close":
                return self._send_json(HTTPStatus.OK, SERVICE.close_ui_session(payload))
            if path == "/api/shutdown":
                reason = str(payload.get("reason", "manual")).strip() or "manual"
                return self._send_json(HTTPStatus.OK, SERVICE.shutdown_client(reason))
        except ValueError as exc:
            return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except SyncClientError as exc:
            logger.warning("POST bridge request failed: path=%s error=%s", path, exc)
            return self._send_error_json(HTTPStatus.BAD_GATEWAY, str(exc))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            logger.warning("POST bridge request failed: path=%s error=%s", path, exc)
            return self._send_error_json(HTTPStatus.BAD_GATEWAY, str(exc))
        except Exception as exc:
            logger.exception("Unhandled POST bridge failure: path=%s", path)
            return self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        self._send_error_json(HTTPStatus.NOT_FOUND, "Route not found.")

    def log_message(self, format: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON body.") from exc

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def _serve_static(self, filename: str, content_type: str) -> None:
        path = WEBUI_DIR / filename
        if not path.exists():
            return self._send_error_json(HTTPStatus.NOT_FOUND, f"Static file '{filename}' not found.")

        if filename == "index.html":
            body = path.read_text(encoding="utf-8").replace("__ASSET_TOKEN__", ASSET_TOKEN).encode("utf-8")
        else:
            body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)


def ensure_webui_dir() -> None:
    if not WEBUI_DIR.exists():
        raise SystemExit(f"Web UI folder not found: {WEBUI_DIR}")


def start_local_server(
    host: str,
    port: int,
    *,
    auto_shutdown_on_last_session_close: bool,
) -> tuple[ThreadingHTTPServer, str]:
    ensure_webui_dir()
    host = normalize_local_bind_host(host)

    try:
        server = ThreadingHTTPServer((host, port), RavenLibWebHandler)
    except OSError as exc:
        raise SystemExit(
            f"Cannot start RavenLib web client on http://{host}:{port}: {exc}. "
            "Another RavenLib client copy may still be using this port."
        ) from exc

    SERVICE.attach_server(server, auto_shutdown_on_last_session_close=auto_shutdown_on_last_session_close)
    app_url = f"http://{host}:{port}"
    logger.info("RavenLib web client v%s started at %s (asset token: %s)", APP_VERSION, app_url, ASSET_TOKEN)
    print(f"RavenLib web client v{APP_VERSION}: {app_url} (asset token: {ASSET_TOKEN})")
    return server, app_url


def serve_local_server(server: ThreadingHTTPServer) -> None:
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping web client")
    finally:
        server.server_close()


def request_server_shutdown(server: ThreadingHTTPServer, reason: str = "manual") -> None:
    logger.info("Stopping RavenLib web client server: %s", reason)
    server.shutdown()


def run_embedded_window(host: str, port: int) -> None:
    host = normalize_local_bind_host(host)
    try:
        import webview
    except ImportError as exc:
        raise SystemExit(
            "pywebview is required for the embedded desktop window. Install it with "
            "'pip install pywebview' in the same Python environment used to run the client."
        ) from exc

    server, app_url = start_local_server(
        host,
        port,
        auto_shutdown_on_last_session_close=True,
    )
    server_thread = threading.Thread(target=serve_local_server, args=(server,), daemon=True)
    server_thread.start()

    api = EmbeddedWindowApi()
    window_icon_path = ensure_window_icon_path()
    try:
        window = webview.create_window(
            f"RavenLib Sync {APP_VERSION}",
            url=f"{app_url}/?v={ASSET_TOKEN}&t={int(time.time())}&window=app",
            js_api=api,
            width=1480,
            height=980,
        )
        api.attach_window(window)

        def on_window_closed(*_args) -> None:
            request_server_shutdown(server, reason="window-closed")

        window.events.closed += on_window_closed
        start_kwargs = {"icon": str(window_icon_path)} if window_icon_path is not None else {}
        try:
            webview.start(**start_kwargs)
        except TypeError:
            logger.warning("pywebview does not support runtime window icons in this environment; starting without icon")
            webview.start()
    finally:
        request_server_shutdown(server, reason="embedded-window-exit")
        server_thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description="RavenLib local web client")
    parser.add_argument("--host", default=LOCAL_BIND_HOST)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-window", action="store_true", help="Serve without opening a window")
    parser.add_argument("--window-browser", default="embedded", help=argparse.SUPPRESS)
    args = parser.parse_args()

    try:
        args.host = normalize_local_bind_host(args.host)
    except ValueError as exc:
        parser.error(str(exc))

    if args.no_window:
        server, _app_url = start_local_server(
            args.host,
            args.port,
            auto_shutdown_on_last_session_close=False,
        )
        serve_local_server(server)
        return

    run_embedded_window(args.host, args.port)


if __name__ == "__main__":
    main()


