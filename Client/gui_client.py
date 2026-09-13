
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import socket
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import os
import time






# GUI logs are printed to the console that starts the application.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ravenlib.client")


def resource_base_dir() -> Path:
    """Return the folder that contains bundled read-only resources."""

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parent


def writable_app_dir() -> Path:
    """Return the folder containing the running app or source file."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# Keep user settings outside the application directory. Versioned PyInstaller
# builds are placed in different folders, while the system temp directory is
# shared by all client versions on the same machine.
DEFAULT_SERVER_URL = "http://127.0.0.1:8000"
CONFIG_DIR = Path(tempfile.gettempdir()) / "RavenLib"
CONFIG_PATH = CONFIG_DIR / "client_config.json"
LEGACY_CONFIG_PATH = writable_app_dir() / "client_config.json"
LOCAL_MANIFEST_DIR_NAME = ".ravenlib"
DEFAULT_STREAM_CHUNK_SIZE_MB = 8
DEFAULT_UPLOAD_TIMEOUT_SECONDS = 600
DEFAULT_UPLOAD_RETRY_ATTEMPTS = 3
DEFAULT_API_TIMEOUT_SECONDS = 60
IGNORED_DIRS = {
    ".git", ".svn", ".hg", "pycache", ".venv", "venv", ".vs", ".idea", ".vscode",
    "Intermediate", "Binaries", "bin", "obj", ".pytest_cache", ".mypy_cache",
    LOCAL_MANIFEST_DIR_NAME,
}
IGNORED_FILE_SUFFIXES = (
    ".vc.db",
    ".suo",
    ".user",
    ".ucas",
    ".utoc",
    ".pak",
    ".pdb",
    ".ilk",
    ".exp",
)

BASE_DIR = writable_app_dir()
MANIFESTS_DIR = BASE_DIR / "manifests"
MANIFESTS_DIR.mkdir(exist_ok=True)


def format_bytes(size: int) -> str:
    """Render byte counts into a compact human-readable form."""

    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(0, size))
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"
    return f"{value:.1f} {units[unit_index]}"


class ProjectManifestBuilder:
    """Scans a project folder and builds a RavenLib JSON manifest."""

    def build(
        self,
        project_name: str,
        project_dir: Path,
        commit_message: str = "",
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> dict:
        """Return manifest: project + files with path, sha256, and size."""

        logger.info("Scanning project: name=%s dir=%s", project_name, project_dir)
        files = []
        candidate_files = [
            file_path
            for file_path in sorted(project_dir.rglob("*"))
            if file_path.is_file() and not self._is_ignored(file_path, project_dir)
        ]
        total_files = len(candidate_files)

        for index, file_path in enumerate(candidate_files, start=1):
            relative_path = file_path.relative_to(project_dir).as_posix()
            stat = file_path.stat()
            files.append(
                {
                    "path": relative_path,
                    "sha256": self._sha256(file_path),
                    "size": stat.st_size,
                }
            )
            if progress_callback is not None:
                progress_callback(index, total_files, relative_path)

        logger.info("Project scan finished: files=%s", len(files))
        return {"project": project_name, "commit_message": commit_message, "files": files}

    def _is_ignored(self, file_path: Path, project_dir: Path) -> bool:
        """Skip service folders that should not be part of the manifest."""

        relative_parts = file_path.relative_to(project_dir).parts
        if any(part in IGNORED_DIRS for part in relative_parts):
            return True

        return file_path.name.lower().endswith(IGNORED_FILE_SUFFIXES)

    def _sha256(self, file_path: Path) -> str:
        """Calculate SHA-256 in chunks so large files are not loaded fully."""

        digest = hashlib.sha256()
        with file_path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


class SyncClientError(Exception):
    """Raised when the sync API returns a user-facing error."""


class SyncApiClient:
    """Owns HTTP communication with the server."""

    def __init__(self, config_store: "ClientConfig | None" = None) -> None:
        self.config_store = config_store or ClientConfig()

    def _decode_http_error(self, exc: HTTPError) -> str:
        """Extract the best available server-side error detail."""

        detail = None
        try:
            body = exc.read()
        except OSError:
            body = b""

        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
                detail = payload.get("detail") or payload.get("error") or payload.get("message")
            except (UnicodeDecodeError, json.JSONDecodeError):
                detail = body.decode("utf-8", errors="replace").strip() or None

        return detail or f"HTTP {exc.code}: {exc.reason}"

    def _get_stream_chunk_size_bytes(self) -> int:
        return self.config_store.stream_chunk_size_bytes()

    def _get_upload_timeout_seconds(self) -> int:
        return self.config_store.upload_timeout_seconds()

    def _get_upload_retry_attempts(self) -> int:
        return self.config_store.upload_retry_attempts()

    def _is_timeout_error(self, exc: BaseException) -> bool:
        current: BaseException | None = exc
        while current is not None:
            if isinstance(current, (TimeoutError, socket.timeout)):
                return True
            if isinstance(current, URLError):
                reason = current.reason
                if isinstance(reason, (TimeoutError, socket.timeout)):
                    return True
                if isinstance(reason, OSError) and "timed out" in str(reason).lower():
                    return True
                if isinstance(reason, str) and "timed out" in reason.lower():
                    return True
            if isinstance(current, OSError) and "timed out" in str(current).lower():
                return True
            current = current.__cause__ or current.__context__
        return False

    def _get_api_timeout_seconds(self) -> int:
        return self.config_store.api_timeout_seconds()

    def _get_long_api_timeout_seconds(self) -> int:
        """Timeout for endpoints that may scan or validate an entire project."""

        return max(300, self._get_api_timeout_seconds(), self._get_upload_timeout_seconds())

    def _request_headers(
        self,
        headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        return dict(headers or {})

    def _open_connection(
        self,
        url: str,
        timeout: int,
    ) -> tuple[http.client.HTTPConnection, str]:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            raise SyncClientError(f"Unsupported server URL scheme: {parsed.scheme or '(empty)'}")

        connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        host = parsed.hostname
        if not host:
            raise SyncClientError("Server URL is missing a hostname.")

        default_port = 443 if parsed.scheme == "https" else 80
        connection = connection_cls(host, parsed.port or default_port, timeout=timeout)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return connection, path

    def _decode_response_error(self, response: http.client.HTTPResponse) -> str:
        body = response.read()
        return self._decode_error_body(response.status, response.reason, body)

    def _decode_error_body(self, status: int, reason: str, body: bytes) -> str:
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
                detail = payload.get("detail") or payload.get("error") or payload.get("message")
                if detail:
                    return str(detail)
            except (UnicodeDecodeError, json.JSONDecodeError):
                text = body.decode("utf-8", errors="replace").strip()
                if text:
                    return text
        return f"HTTP {status}: {reason}"

    def _emit_upload_audit(
        self,
        stage: str,
        sha256: str,
        file_path: Path,
        attempt: int,
        log_callback: Callable[[str], None] | None = None,
        **details: object,
    ) -> None:
        detail_text = " ".join(f"{key}={value}" for key, value in details.items())
        message = (
            f"[UPLOAD-AUDIT] {stage} | file={file_path.name} sha256={sha256} attempt={attempt}"
            f"{(' ' + detail_text) if detail_text else ''}"
        )
        logger.info(message)
        if log_callback is not None:
            log_callback(message)

    def _request_json(self, request: Request, timeout: int | None = None, allow_404: bool = False) -> dict | None:
        """Run an HTTP request and parse a JSON response."""

        resolved_timeout = max(1, int(timeout)) if timeout is not None else self._get_api_timeout_seconds()
        try:
            with urlopen(request, timeout=resolved_timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            raise SyncClientError(self._decode_http_error(exc)) from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            if self._is_timeout_error(exc):
                raise SyncClientError(f"Request timed out after {resolved_timeout} seconds.") from exc
            raise SyncClientError(str(exc)) from exc

    def send_manifest(self, server_url: str, manifest: dict) -> dict:
        """Send a manifest to the server and return the parsed JSON response."""

        logger.info(
            "Sending manifest: server=%s project=%s files=%s",
            server_url,
            manifest.get("project"),
            len(manifest.get("files", [])),
        )
        request = Request(
            f"{server_url.rstrip('/')}/sync/check",
            data=json.dumps(manifest).encode("utf-8"),
            headers=self._request_headers({"Content-Type": "application/json"}),
            method="POST",
        )
        parsed_response = self._request_json(request, timeout=self._get_long_api_timeout_seconds())

        logger.info("Server response received: %s", parsed_response)
        return parsed_response

    def finalize_sync(self, server_url: str, sync_id: str) -> dict:
        """Finalize a staged sync only after every required object is uploaded."""

        request = Request(
            f"{server_url.rstrip('/')}/sync/finalize/{quote(sync_id, safe='')}",
            headers=self._request_headers(),
            method="POST",
        )
        response = self._request_json(request, timeout=self._get_long_api_timeout_seconds())
        logger.info("Sync finalized: sync_id=%s", sync_id)
        return response

    def fetch_latest_manifest(self, server_url: str, project: str) -> dict | None:
        """Fetch the server's current manifest for a project, or None if unknown yet.

        This lets the client compare its local state against what any
        computer last pushed, instead of only against its own history.
        """

        logger.info("Fetching latest manifest: server=%s project=%s", server_url, project)
        request = Request(
            f"{server_url.rstrip('/')}/sync/manifest/{quote(project, safe='')}",
            headers=self._request_headers(),
            method="GET",
        )
        response = self._request_json(
            request,
            timeout=self._get_long_api_timeout_seconds(),
            allow_404=True,
        )
        if response is None:
            logger.info("No manifest known on server yet for project=%s", project)
        return response

    def upload_object(self, server_url: str, sha256: str, data: bytes, sync_id: str | None = None) -> dict:
        """Upload one file's raw content, addressed by its sha256."""

        url = f"{server_url.rstrip('/')}/objects/{sha256}"
        if sync_id:
            url = f"{url}?sync_id={quote(sync_id, safe='')}"
        connection, path = self._open_connection(url, timeout=60)
        try:
            connection.putrequest("PUT", path)
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(len(data)))
            connection.endheaders()
            connection.send(data)

            response = connection.getresponse()
            body = response.read()
            if response.status >= 400:
                raise SyncClientError(self._decode_error_body(response.status, response.reason, body))
            return json.loads(body.decode("utf-8"))
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise SyncClientError(str(exc)) from exc
        finally:
            connection.close()

    def download_object(self, server_url: str, sha256: str) -> bytes:
        """Download one file's raw content by its sha256."""

        url = f"{server_url.rstrip('/')}/objects/{sha256}"
        connection, path = self._open_connection(url, timeout=60)
        chunk_size = self._get_stream_chunk_size_bytes()
        chunks = bytearray()
        try:
            connection.request("GET", path, headers=self._request_headers())
            response = connection.getresponse()
            if response.status >= 400:
                raise SyncClientError(self._decode_response_error(response))
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                chunks.extend(chunk)
            return bytes(chunks)
        except (OSError, TimeoutError) as exc:
            raise SyncClientError(str(exc)) from exc
        finally:
            connection.close()

    def upload_object_file(
        self,
        server_url: str,
        sha256: str,
        file_path: Path,
        sync_id: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        log_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        retry_attempts: int | None = None,
    ) -> dict:
        """Upload one object by streaming it from disk in small chunks."""

        url = f"{server_url.rstrip('/')}/objects/{sha256}"
        if sync_id:
            url = f"{url}?sync_id={quote(sync_id, safe='')}"

        chunk_size = self._get_stream_chunk_size_bytes()
        file_size = file_path.stat().st_size
        resolved_timeout = max(1, int(timeout)) if timeout is not None else self._get_upload_timeout_seconds()
        max_attempts = max(1, int(retry_attempts)) if retry_attempts is not None else self._get_upload_retry_attempts()

        for attempt in range(1, max_attempts + 1):
            connection, path = self._open_connection(url, timeout=resolved_timeout)
            response: http.client.HTTPResponse | None = None
            logger.info(
                "Streaming upload started: path=%s sha256=%s size=%s chunk_size=%s attempt=%s/%s timeout=%ss",
                file_path,
                sha256,
                file_size,
                chunk_size,
                attempt,
                max_attempts,
                resolved_timeout,
            )
            if log_callback is not None:
                if attempt == 1:
                    log_callback(f"Streaming upload started: {file_path.name} ({format_bytes(file_size)})")
                else:
                    log_callback(
                        f"Retrying upload from start: {file_path.name} "
                        f"(attempt {attempt}/{max_attempts})"
                    )

            try:
                connection.putrequest("PUT", path)
                connection.putheader("Content-Type", "application/octet-stream")
                connection.putheader("Content-Length", str(file_size))
                connection.endheaders()

                sent = 0
                chunk_index = 0
                with file_path.open("rb") as handle:
                    while True:
                        chunk = handle.read(chunk_size)
                        if not chunk:
                            break
                        connection.send(chunk)
                        sent += len(chunk)
                        chunk_index += 1
                        logger.info(
                            "Chunk written: sha256=%s bytes=%s total=%s attempt=%s/%s",
                            sha256,
                            len(chunk),
                            sent,
                            attempt,
                            max_attempts,
                        )
                        if progress_callback is not None:
                            progress_callback(sent, file_size)
                        if log_callback is not None and (
                            chunk_index == 1 or chunk_index % 32 == 0 or sent == file_size
                        ):
                            log_callback(
                                f"Chunk written: {file_path.name} "
                                f"{format_bytes(sent)} / {format_bytes(file_size)}"
                            )

                response = connection.getresponse()
                self._emit_upload_audit(
                    "HTTP response received",
                    sha256,
                    file_path,
                    attempt,
                    log_callback,
                    status=response.status,
                    reason=response.reason,
                )
                body = response.read()
                self._emit_upload_audit(
                    "response body read",
                    sha256,
                    file_path,
                    attempt,
                    log_callback,
                    bytes=len(body),
                )
                if response.status >= 400:
                    raise SyncClientError(self._decode_error_body(response.status, response.reason, body))
                payload = json.loads(body.decode("utf-8"))
                logger.info(
                    "Upload completed: path=%s sha256=%s size=%s attempts_used=%s",
                    file_path,
                    sha256,
                    file_size,
                    attempt,
                )
                if log_callback is not None:
                    log_callback(f"Upload completed: {file_path.name} ({format_bytes(file_size)})")
                return payload
            except SyncClientError:
                raise
            except (OSError, TimeoutError, json.JSONDecodeError) as exc:
                if self._is_timeout_error(exc) and attempt < max_attempts:
                    logger.warning(
                        "Upload timed out, restarting from byte 0: path=%s sha256=%s attempt=%s/%s error=%s",
                        file_path,
                        sha256,
                        attempt,
                        max_attempts,
                        exc,
                    )
                    if progress_callback is not None:
                        progress_callback(0, file_size)
                    if log_callback is not None:
                        log_callback(
                            f"Upload timed out: {file_path.name}. "
                            f"Retrying from start ({attempt + 1}/{max_attempts})."
                        )
                    time.sleep(1)
                    continue
                raise SyncClientError(str(exc)) from exc
            finally:
                if response is not None:
                    response.close()
                    self._emit_upload_audit(
                        "response object closed",
                        sha256,
                        file_path,
                        attempt,
                        log_callback,
                    )
                connection.close()
                self._emit_upload_audit(
                    "connection closed",
                    sha256,
                    file_path,
                    attempt,
                    log_callback,
                )

        raise SyncClientError(f"Upload failed for {file_path.name}.")

    def download_object_to_path(
        self,
        server_url: str,
        sha256: str,
        destination: Path,
        progress_callback: Callable[[int, int], None] | None = None,
        log_callback: Callable[[str], None] | None = None,
        timeout: int = 600,
    ) -> dict:
        """Download one object by streaming it directly to a temporary file on disk."""

        url = f"{server_url.rstrip('/')}/objects/{sha256}"
        connection, path = self._open_connection(url, timeout=timeout)
        chunk_size = self._get_stream_chunk_size_bytes()
        temp_path = destination.with_name(f".{destination.name}.{os.getpid()}.{threading.get_ident()}.downloading")
        digest = hashlib.sha256()
        received = 0

        try:
            connection.request("GET", path, headers=self._request_headers())
            response = connection.getresponse()
            if response.status >= 400:
                raise SyncClientError(self._decode_response_error(response))

            content_length = int(response.getheader("Content-Length") or 0)
            logger.info(
                "Streaming download started: destination=%s sha256=%s expected_size=%s chunk_size=%s",
                destination,
                sha256,
                content_length,
                chunk_size,
            )
            if log_callback is not None:
                size_text = format_bytes(content_length) if content_length else "unknown size"
                log_callback(f"Streaming download started: {destination.name} ({size_text})")

            chunk_index = 0
            with temp_path.open("wb") as handle:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    chunk_index += 1
                    logger.info("Chunk received: sha256=%s bytes=%s total=%s", sha256, len(chunk), received)
                    if progress_callback is not None:
                        progress_callback(received, content_length or received)
                    if log_callback is not None and (chunk_index == 1 or chunk_index % 32 == 0 or (content_length and received == content_length)):
                        if content_length:
                            progress_text = f"{format_bytes(received)} / {format_bytes(content_length)}"
                        else:
                            progress_text = format_bytes(received)
                        log_callback(f"Chunk received: {destination.name} {progress_text}")

            actual_sha256 = digest.hexdigest()
            if actual_sha256 != sha256.lower():
                temp_path.unlink(missing_ok=True)
                raise SyncClientError(
                    f"Downloaded object '{sha256}' failed SHA256 verification (actual: {actual_sha256})."
                )

            logger.info("SHA verified: sha256=%s size=%s", sha256, received)
            if log_callback is not None:
                log_callback(f"SHA verified: {destination.name} ({format_bytes(received)})")

            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp_path, destination)
            logger.info("Download completed: destination=%s sha256=%s size=%s", destination, sha256, received)
            if log_callback is not None:
                log_callback(f"Download completed: {destination.name} ({format_bytes(received)})")
            return {"size": received, "sha256": actual_sha256}
        except (OSError, TimeoutError) as exc:
            temp_path.unlink(missing_ok=True)
            raise SyncClientError(str(exc)) from exc
        finally:
            connection.close()




class LocalManifestStore:
    """Persists the locally known 'current' manifest for a project.

    This is the client-side equivalent of a git index: after a successful
    sync, the sent manifest is saved here so the next scan can be diffed
    against it to see what changed, instead of resending everything blind.
    """

    def _path(self, project_dir: Path) -> Path:
        return project_dir / LOCAL_MANIFEST_DIR_NAME / "manifest.json"

    def load(self, project_dir: Path) -> dict | None:
        """Return the last saved manifest for this project folder, or None."""

        path = self._path(project_dir)
        if not path.exists():
            return None

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Local current manifest is invalid, ignoring: %s", path)
            return None

    def save(self, project_dir: Path, manifest: dict) -> None:
        """Save the manifest as the new locally known current state."""

        path = self._path(project_dir)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(manifest, indent=4, ensure_ascii=False), encoding="utf-8")
        logger.info("Local current manifest saved: %s", path)

    def clear(self, project_dir: Path) -> None:
        """Remove the locally stored manifest after a server reset."""

        path = self._path(project_dir)
        if not path.exists():
            return

        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        logger.info("Local current manifest removed: %s", path)


class ManifestDiffer:
    """Compares two manifests by path/sha256 to find what changed."""

    def diff(self, old_manifest: dict | None, new_manifest: dict) -> dict:
        """Return added/modified/removed file paths and an unchanged count."""

        old_files = {f["path"]: f["sha256"] for f in (old_manifest or {}).get("files", [])}
        new_files = {f["path"]: f["sha256"] for f in new_manifest.get("files", [])}

        added = sorted(path for path in new_files if path not in old_files)
        removed = sorted(path for path in old_files if path not in new_files)
        modified = sorted(
            path for path in new_files
            if path in old_files and new_files[path] != old_files[path]
        )
        unchanged = len(new_files) - len(added) - len(modified)

        return {
            "added": added,
            "modified": modified,
            "removed": removed,
            "unchanged": unchanged,
        }


class ClientConfig:
    """Read and save client settings in a version-independent temp folder."""

    @staticmethod
    def _read_file(path: Path) -> dict | None:
        """Read one config file, returning None when it cannot be used."""

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.warning("Config file is invalid or inaccessible: %s", path)
            return None

        if not isinstance(payload, dict):
            logger.warning("Config file must contain a JSON object: %s", path)
            return None
        return payload

    @staticmethod
    def _write_file(payload: dict) -> None:
        """Atomically write a config file, including when several clients run."""

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=CONFIG_DIR,
                prefix=f".{CONFIG_PATH.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                json.dump(payload, temp_file, indent=4, ensure_ascii=False)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, CONFIG_PATH)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def load(self) -> dict:
        """Load settings, returning defaults when the config file is missing or invalid."""

        if CONFIG_PATH.exists():
            payload = self._read_file(CONFIG_PATH)
            if payload is not None:
                return payload

        if LEGACY_CONFIG_PATH.exists() and LEGACY_CONFIG_PATH != CONFIG_PATH:
            payload = self._read_file(LEGACY_CONFIG_PATH)
            if payload is not None:
                try:
                    self._write_file(payload)
                    logger.info("Migrated legacy config to %s", CONFIG_PATH)
                except OSError:
                    logger.warning("Could not migrate legacy config to %s", CONFIG_PATH)
                return payload
        return {}

    def _read_positive_int(self, key: str, default: int) -> int:
        config = self.load()
        raw_value = config.get(key, default)
        try:
            return max(1, int(raw_value))
        except (TypeError, ValueError):
            return default

    def stream_chunk_size_bytes(self) -> int:
        chunk_size_mb = self._read_positive_int("stream_chunk_size_mb", DEFAULT_STREAM_CHUNK_SIZE_MB)
        return chunk_size_mb * 1024 * 1024

    def upload_timeout_seconds(self) -> int:
        return self._read_positive_int("upload_timeout_seconds", DEFAULT_UPLOAD_TIMEOUT_SECONDS)

    def upload_retry_attempts(self) -> int:
        return self._read_positive_int("upload_retry_attempts", DEFAULT_UPLOAD_RETRY_ATTEMPTS)

    def api_timeout_seconds(self) -> int:
        return self._read_positive_int("api_timeout_seconds", DEFAULT_API_TIMEOUT_SECONDS)

    def save(self, data: dict) -> None:
        """Save selected server URL, project name, and project folder."""

        payload = self.load()
        payload.update(data)
        payload.pop("server_password", None)
        try:
            self._write_file(payload)
        except OSError:
            logger.exception("Could not save config: %s", CONFIG_PATH)

