import hashlib
import base64
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "server.log"
MANIFESTS_DIR = BASE_DIR / "manifests"
MANIFESTS_DIR.mkdir(exist_ok=True)
LATEST_DIR = MANIFESTS_DIR / "latest"
LATEST_DIR.mkdir(exist_ok=True)
PENDING_DIR = MANIFESTS_DIR / "pending"
PENDING_DIR.mkdir(exist_ok=True)
OBJECTS_DIR = BASE_DIR / "objects"
OBJECTS_DIR.mkdir(exist_ok=True)
SERVER_WEB_DIR = BASE_DIR / "web"
SERVER_CONFIG_PATH = BASE_DIR / "server_config.json"
STARTED_AT = datetime.now(timezone.utc)
STATE_LOCK = threading.Lock()
DEFAULT_STREAM_CHUNK_SIZE_MB = 8
PASSWORD_HASH_ITERATIONS = 390000
PASSWORD_SALT_BYTES = 16
MIN_SERVER_PASSWORD_LENGTH = 8
DASHBOARD_SESSION_TTL_HOURS = 12
AUTH_BEARER_PREFIX = "Bearer "


def setup_logging() -> logging.Logger:
    """Configure console + file logging and return the project logger."""

    log_format = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_format)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(log_format))

    server_logger = logging.getLogger("ravenlib.server")
    if not any(isinstance(handler, logging.FileHandler) for handler in server_logger.handlers):
        server_logger.addHandler(file_handler)
    server_logger.setLevel(logging.INFO)
    return server_logger


logger = setup_logging()
app = FastAPI(title="RavenLib Server")


class ConnectionStatus(BaseModel):
    connected: bool
    message: str
    server_time: datetime


class ManifestFile(BaseModel):
    path: str
    sha256: str = Field(min_length=64, max_length=64)
    size: int = Field(ge=0)


class SyncCheckRequest(BaseModel):
    project: str
    commit_message: str = ""
    files: list[ManifestFile]


class SyncCheckResponse(BaseModel):
    accepted: bool
    message: str
    received_at: datetime
    sender: str
    manifest_file: str
    commit_message: str = ""
    need_upload: list[str]
    sync_id: str


class SyncFinalizeResponse(BaseModel):
    accepted: bool
    message: str
    received_at: datetime
    sender: str
    manifest_file: str
    commit_message: str = ""
    uploaded_objects: int = 0
    object_count: int = 0
    sync_id: str | None = None


class LatestManifestResponse(BaseModel):
    project: str
    updated_at: datetime
    sender: str
    commit_message: str = ""
    files: list[ManifestFile]


class CommitSummary(BaseModel):
    commit_id: str
    project: str
    sender: str
    received_at: datetime
    files: int
    commit_message: str = ""
    is_rollback: bool = False
    reverted_from: str | None = None


class ObjectUploadResponse(BaseModel):
    sha256: str
    stored: bool
    size: int
    sync_id: str | None = None


class ProjectIntegrityIssue(BaseModel):
    sha256: str
    files: list[str]
    error: str
    actual_sha256: str | None = None


class ProjectIntegrityResponse(BaseModel):
    project: str
    file_count: int
    object_count: int
    missing_objects: int
    corrupted_objects: int
    status: str
    missing_details: list[ProjectIntegrityIssue]
    corrupted_details: list[ProjectIntegrityIssue]
    checked_at: datetime


class ProjectResetResponse(BaseModel):
    project: str
    removed_commits: int
    removed_pending_syncs: int
    removed_latest_manifest: bool
    removed_objects: int
    checked_at: datetime
    message: str


class DashboardStatus(BaseModel):
    connected: bool
    message: str
    server_time: datetime
    started_at: datetime
    uptime_seconds: int
    projects: int
    commits: int
    objects: int


class DashboardCommitList(BaseModel):
    items: list[CommitSummary]


class DashboardLogs(BaseModel):
    lines: list[str]


class DashboardProjectSummary(BaseModel):
    project: str
    updated_at: datetime
    sender: str
    commit_message: str = ""
    status: str
    file_count: int
    object_count: int
    total_size: int
    commit_count: int
    pending_syncs: int


class DashboardProjectList(BaseModel):
    items: list[DashboardProjectSummary]


class ProjectNameList(BaseModel):
    items: list[str]


class DashboardProjectDetail(BaseModel):
    project: str
    updated_at: datetime
    sender: str
    commit_message: str = ""
    status: str
    file_count: int
    object_count: int
    total_size: int
    directory_count: int
    commit_count: int
    pending_syncs: int
    missing_objects: int
    corrupted_objects: int
    checked_at: datetime
    latest_manifest_file: str
    latest_commit_id: str | None = None
    files: list[ManifestFile]
    commits: list[CommitSummary]


class AuthStatusResponse(BaseModel):
    password_configured: bool
    requires_bootstrap: bool
    minimum_password_length: int
    dashboard_session_ttl_hours: int
    server_time: datetime


class AuthLoginRequest(BaseModel):
    password: str = Field(min_length=1)


class AuthBootstrapRequest(BaseModel):
    new_password: str = Field(min_length=1)


class AuthLoginResponse(BaseModel):
    authenticated: bool
    message: str
    session_token: str
    expires_at: datetime


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


class PasswordChangeResponse(BaseModel):
    changed: bool
    message: str
    session_token: str
    expires_at: datetime


def requires_dashboard_auth(path: str) -> bool:
    return path == "/api/auth/change-password" or path.startswith("/api/dashboard/")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every HTTP request so it is obvious whether it reached the server."""

    started_at = time.perf_counter()
    sender = get_sender_identifier(request)
    logger.info("Request started: %s %s from %s", request.method, request.url.path, sender)

    try:
        enforce_request_auth(request)
    except HTTPException as exc:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.warning(
            "Request rejected: %s %s from %s -> %s in %.2f ms (%s)",
            request.method,
            request.url.path,
            sender,
            exc.status_code,
            elapsed_ms,
            exc.detail,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers or {},
        )

    response = await call_next(request)

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    logger.info(
        "Request finished: %s %s from %s -> %s in %.2f ms",
        request.method,
        request.url.path,
        sender,
        response.status_code,
        elapsed_ms,
    )
    return response


def get_sender_identifier(request: Request) -> str:
    """Return the best available client identifier for logs and filenames."""

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()

    if request.client:
        return request.client.host

    return "unknown-client"


def sanitize_filename_part(value: str) -> str:
    """Make an IP or other identifier safe to use inside a filename."""

    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return sanitized.strip("._") or "unknown-client"


def atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically so readers never see a partial manifest."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(json.dumps(payload, indent=4, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)


def latest_manifest_path(project: str) -> Path:
    return LATEST_DIR / f"{sanitize_filename_part(project)}.json"


def pending_sync_path(sync_id: str) -> Path:
    return PENDING_DIR / f"{sanitize_filename_part(sync_id)}.json"


def object_path(sha256: str) -> Path:
    sha256 = sha256.lower()
    return OBJECTS_DIR / sha256[:2] / sha256


def load_server_config() -> dict:
    if not SERVER_CONFIG_PATH.exists():
        return {}

    try:
        return json.loads(SERVER_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Server config is invalid, using defaults: %s", SERVER_CONFIG_PATH)
        return {}


def save_server_config(payload: dict) -> None:
    atomic_write_json(SERVER_CONFIG_PATH, payload)


def load_server_auth_config() -> dict:
    config = load_server_config()
    auth_config = config.get("auth", {})
    return auth_config if isinstance(auth_config, dict) else {}


def is_server_password_configured() -> bool:
    auth_config = load_server_auth_config()
    return bool(auth_config.get("password_hash") and auth_config.get("password_salt"))


def normalize_server_password(password: str) -> str:
    return password.strip()


def validate_server_password(password: str) -> str:
    normalized = normalize_server_password(password)
    if len(normalized) < MIN_SERVER_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {MIN_SERVER_PASSWORD_LENGTH} characters long.",
        )
    return normalized


def hash_server_password(password: str, salt_hex: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        iterations,
    ).hex()


def build_server_password_payload(password: str) -> dict:
    salt_hex = secrets.token_hex(PASSWORD_SALT_BYTES)
    iterations = PASSWORD_HASH_ITERATIONS
    hashed = hash_server_password(password, salt_hex, iterations)
    return {
        "password_hash": hashed,
        "password_salt": salt_hex,
        "password_iterations": iterations,
        "password_updated_at": datetime.now(timezone.utc).isoformat(),
    }


def verify_server_password(password: str) -> bool:
    auth_config = load_server_auth_config()
    password_hash = str(auth_config.get("password_hash", ""))
    salt_hex = str(auth_config.get("password_salt", ""))
    if not password_hash or not salt_hex:
        return False

    try:
        iterations = max(1, int(auth_config.get("password_iterations", PASSWORD_HASH_ITERATIONS)))
        candidate_hash = hash_server_password(normalize_server_password(password), salt_hex, iterations)
    except (TypeError, ValueError):
        return False
    except ValueError:
        logger.warning("Server password salt is invalid; rejecting authentication.")
        return False

    return hmac.compare_digest(candidate_hash, password_hash)


def _dashboard_session_secret() -> bytes | None:
    auth_config = load_server_auth_config()
    password_hash = str(auth_config.get("password_hash", "")).strip()
    salt_hex = str(auth_config.get("password_salt", "")).strip()
    if not password_hash or not salt_hex:
        return None
    return f"{password_hash}:{salt_hex}".encode("utf-8")


def _urlsafe_b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _urlsafe_b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(f"{data}{padding}")


def create_dashboard_session() -> tuple[str, datetime]:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=DASHBOARD_SESSION_TTL_HOURS)
    secret = _dashboard_session_secret()
    if secret is None:
        raise RuntimeError("Server password is not configured correctly; cannot issue dashboard sessions.")

    payload = json.dumps(
        {"exp": expires_at.timestamp(), "nonce": secrets.token_urlsafe(12)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload_part = _urlsafe_b64encode(payload)
    signature_part = _urlsafe_b64encode(hmac.digest(secret, payload_part.encode("ascii"), "sha256"))
    session_token = f"{payload_part}.{signature_part}"
    return session_token, expires_at


def clear_dashboard_sessions() -> None:
    """Legacy no-op kept for compatibility with the previous in-memory session store."""
    return None


def is_dashboard_session_valid(session_token: str) -> bool:
    if not session_token:
        return False

    secret = _dashboard_session_secret()
    if secret is None:
        return False

    try:
        payload_part, signature_part = session_token.split(".", 1)
        expected_signature = _urlsafe_b64encode(hmac.digest(secret, payload_part.encode("ascii"), "sha256"))
        if not hmac.compare_digest(signature_part, expected_signature):
            return False

        payload = json.loads(_urlsafe_b64decode(payload_part).decode("utf-8"))
        expires_at = float(payload.get("exp", 0))
        return expires_at > time.time()
    except (ValueError, TypeError, json.JSONDecodeError, OSError):
        return False


def extract_bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.startswith(AUTH_BEARER_PREFIX):
        return authorization[len(AUTH_BEARER_PREFIX):].strip()
    return ""


def update_server_password(password: str) -> None:
    config = load_server_config()
    config["auth"] = build_server_password_payload(password)
    save_server_config(config)
    clear_dashboard_sessions()


def enforce_request_auth(request: Request) -> None:
    if request.method == "OPTIONS" or not requires_dashboard_auth(request.url.path):
        return

    if not is_server_password_configured():
        raise HTTPException(
            status_code=503,
            detail="Server password is not configured yet. Open the dashboard and set it first.",
        )

    bearer_token = extract_bearer_token(request)
    if bearer_token and is_dashboard_session_valid(bearer_token):
        return

    raise HTTPException(
        status_code=401,
        detail="Dashboard authentication required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_stream_chunk_size_bytes() -> int:
    config = load_server_config()
    raw_value = config.get("stream_chunk_size_mb", DEFAULT_STREAM_CHUNK_SIZE_MB)
    try:
        chunk_size_mb = max(1, int(raw_value))
    except (TypeError, ValueError):
        chunk_size_mb = DEFAULT_STREAM_CHUNK_SIZE_MB
    return chunk_size_mb * 1024 * 1024


def manifest_unique_hashes(manifest: SyncCheckRequest) -> list[str]:
    return sorted({file.sha256.lower() for file in manifest.files})


def manifest_hash_to_files(manifest: SyncCheckRequest) -> dict[str, list[str]]:
    hash_to_files: dict[str, list[str]] = {}
    for file in manifest.files:
        hash_to_files.setdefault(file.sha256.lower(), []).append(file.path)
    return hash_to_files


def compute_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_integrity_issue(
    sha256: str,
    files: list[str],
    error: str,
    actual_sha256: str | None = None,
) -> ProjectIntegrityIssue:
    return ProjectIntegrityIssue(
        sha256=sha256,
        files=files,
        error=error,
        actual_sha256=actual_sha256,
    )


def verify_manifest_integrity(
    project: str,
    manifest: SyncCheckRequest,
    checked_at: datetime | None = None,
) -> ProjectIntegrityResponse:
    """Verify that every object referenced by the manifest exists and matches its hash."""

    checked_at = checked_at or datetime.now(timezone.utc)
    hash_to_files = manifest_hash_to_files(manifest)
    missing_details: list[ProjectIntegrityIssue] = []
    corrupted_details: list[ProjectIntegrityIssue] = []

    for sha256, files in sorted(hash_to_files.items()):
        stored_object = object_path(sha256)
        if not stored_object.exists():
            missing_details.append(build_integrity_issue(sha256, files, "Object file is missing on server."))
            continue

        actual_sha256 = compute_file_sha256(stored_object)
        if actual_sha256 != sha256:
            corrupted_details.append(
                build_integrity_issue(
                    sha256,
                    files,
                    "Stored object hash does not match manifest SHA256.",
                    actual_sha256=actual_sha256,
                )
            )

    return ProjectIntegrityResponse(
        project=project,
        file_count=len(manifest.files),
        object_count=len(hash_to_files),
        missing_objects=len(missing_details),
        corrupted_objects=len(corrupted_details),
        status="Healthy" if not missing_details and not corrupted_details else "Corrupted",
        missing_details=missing_details,
        corrupted_details=corrupted_details,
        checked_at=checked_at,
    )


def ensure_manifest_integrity(project: str, manifest: SyncCheckRequest, context: str) -> ProjectIntegrityResponse:
    """Validate manifest integrity and raise a clear server-side error when it is broken."""

    report = verify_manifest_integrity(project, manifest)
    if report.status == "Healthy":
        return report

    message = (
        f"{context} for project '{project}' is corrupted on server. "
        f"Missing objects: {report.missing_objects}. Corrupted objects: {report.corrupted_objects}. "
        "Run Verify Server Integrity for details."
    )
    raise HTTPException(status_code=409, detail=message)


def verify_manifest_object_presence(
    project: str,
    manifest: SyncCheckRequest,
    checked_at: datetime | None = None,
) -> ProjectIntegrityResponse:
    """Verify only that every referenced object exists on disk."""

    checked_at = checked_at or datetime.now(timezone.utc)
    hash_to_files = manifest_hash_to_files(manifest)
    missing_details: list[ProjectIntegrityIssue] = []

    for sha256, files in sorted(hash_to_files.items()):
        if not object_path(sha256).exists():
            missing_details.append(build_integrity_issue(sha256, files, "Object file is missing on server."))

    return ProjectIntegrityResponse(
        project=project,
        file_count=len(manifest.files),
        object_count=len(hash_to_files),
        missing_objects=len(missing_details),
        corrupted_objects=0,
        status="Healthy" if not missing_details else "Corrupted",
        missing_details=missing_details,
        corrupted_details=[],
        checked_at=checked_at,
    )


def ensure_manifest_objects_present(project: str, manifest: SyncCheckRequest, context: str) -> ProjectIntegrityResponse:
    """Validate only object presence for hot paths like latest/finalize."""

    report = verify_manifest_object_presence(project, manifest)
    if report.status == "Healthy":
        return report

    message = (
        f"{context} for project '{project}' is incomplete on server. "
        f"Missing objects: {report.missing_objects}. "
        "Run Verify Server Integrity for a full byte-level validation report."
    )
    raise HTTPException(status_code=409, detail=message)


def save_manifest(
    manifest: SyncCheckRequest,
    sender: str,
    received_at: datetime,
    is_rollback: bool = False,
    reverted_from: str | None = None,
) -> Path:
    """Save a timestamped immutable history entry for the project."""

    timestamp = received_at.strftime("%Y%m%dT%H%M%S%fZ")
    sender_part = sanitize_filename_part(sender)
    manifest_path = MANIFESTS_DIR / f"{timestamp}_{sender_part}.json"
    payload = {
        "received_at": received_at.isoformat(),
        "sender": sender,
        "manifest": manifest.model_dump(),
        "is_rollback": is_rollback,
        "reverted_from": reverted_from,
    }
    atomic_write_json(manifest_path, payload)
    logger.info("Manifest saved: %s", manifest_path)
    return manifest_path


def save_latest_manifest(manifest: SyncCheckRequest, sender: str, received_at: datetime) -> Path:
    """Save the current latest manifest for the project."""

    path = latest_manifest_path(manifest.project)
    payload = {
        "project": manifest.project,
        "updated_at": received_at.isoformat(),
        "sender": sender,
        "commit_message": manifest.commit_message,
        "files": [file.model_dump() for file in manifest.files],
    }
    atomic_write_json(path, payload)
    logger.info("Latest manifest updated: project=%s path=%s", manifest.project, path)
    return path


def commit_manifest_as_latest(
    manifest: SyncCheckRequest,
    sender: str,
    received_at: datetime,
    *,
    is_rollback: bool = False,
    reverted_from: str | None = None,
) -> Path:
    """Write history and latest together so latest is never published early."""

    manifest_path: Path | None = None
    try:
        manifest_path = save_manifest(
            manifest,
            sender,
            received_at,
            is_rollback=is_rollback,
            reverted_from=reverted_from,
        )
        save_latest_manifest(manifest, sender, received_at)
        return manifest_path
    except Exception:
        if manifest_path is not None and manifest_path.exists():
            manifest_path.unlink(missing_ok=True)
            logger.warning("Rolled back commit file after latest update failure: %s", manifest_path)
        raise


def create_pending_sync(
    manifest: SyncCheckRequest,
    sender: str,
    received_at: datetime,
    need_upload: list[str],
) -> tuple[str, Path]:
    """Create a staged sync record that is finalized only after all objects exist."""

    sync_id = uuid.uuid4().hex
    path = pending_sync_path(sync_id)
    payload = {
        "sync_id": sync_id,
        "received_at": received_at.isoformat(),
        "sender": sender,
        "manifest": manifest.model_dump(),
        "need_upload": sorted({sha.lower() for sha in need_upload}),
        "uploaded_hashes": [],
    }
    atomic_write_json(path, payload)
    logger.info(
        "Pending sync created: sync_id=%s project=%s need_upload=%s",
        sync_id,
        manifest.project,
        len(payload["need_upload"]),
    )
    return sync_id, path


def load_pending_sync_payload(sync_id: str) -> dict:
    path = pending_sync_path(sync_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Pending sync '{sync_id}' not found")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.exception("Pending sync file is corrupted: %s", path)
        raise HTTPException(status_code=500, detail="Pending sync file is corrupted") from exc


def save_pending_sync_payload(sync_id: str, payload: dict) -> None:
    atomic_write_json(pending_sync_path(sync_id), payload)


def delete_pending_sync(sync_id: str) -> None:
    pending_sync_path(sync_id).unlink(missing_ok=True)


def update_pending_uploaded_hash(sync_id: str, sha256: str) -> None:
    payload = load_pending_sync_payload(sync_id)
    uploaded_hashes = set(payload.get("uploaded_hashes", []))
    uploaded_hashes.add(sha256.lower())
    payload["uploaded_hashes"] = sorted(uploaded_hashes)
    save_pending_sync_payload(sync_id, payload)


def iter_project_commit_files():
    """Yield immutable commit files stored directly under manifests/."""

    for path in sorted(MANIFESTS_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Skipping corrupted commit file: %s", path)
            continue
        yield path, payload


def iter_pending_sync_files():
    """Yield pending sync payloads."""

    for path in sorted(PENDING_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Skipping corrupted pending sync file: %s", path)
            continue
        yield path, payload


def list_project_commits(project: str) -> list[CommitSummary]:
    commits = []
    for path, payload in iter_project_commit_files():
        manifest = payload.get("manifest", {})
        if manifest.get("project") != project:
            continue
        commits.append(
            CommitSummary(
                commit_id=path.stem,
                project=project,
                sender=payload.get("sender", "unknown"),
                received_at=payload["received_at"],
                files=len(manifest.get("files", [])),
                commit_message=manifest.get("commit_message", ""),
                is_rollback=payload.get("is_rollback", False),
                reverted_from=payload.get("reverted_from"),
            )
        )
    commits.sort(key=lambda commit: commit.received_at, reverse=True)
    return commits


def list_recent_commits(limit: int = 50) -> list[CommitSummary]:
    commits = []
    for path, payload in iter_project_commit_files():
        manifest = payload.get("manifest", {})
        project = manifest.get("project")
        if not project:
            continue
        commits.append(
            CommitSummary(
                commit_id=path.stem,
                project=project,
                sender=payload.get("sender", "unknown"),
                received_at=payload["received_at"],
                files=len(manifest.get("files", [])),
                commit_message=manifest.get("commit_message", ""),
                is_rollback=payload.get("is_rollback", False),
                reverted_from=payload.get("reverted_from"),
            )
        )
    commits.sort(key=lambda commit: commit.received_at, reverse=True)
    return commits[:limit]


def count_projects() -> int:
    return len(list(LATEST_DIR.glob("*.json")))


def count_stored_objects() -> int:
    return sum(1 for path in OBJECTS_DIR.rglob("*") if path.is_file())


def build_manifest_from_latest_payload(payload: dict) -> SyncCheckRequest:
    return SyncCheckRequest(
        project=payload["project"],
        commit_message=payload.get("commit_message", ""),
        files=payload.get("files", []),
    )


def split_manifest_path(path: str) -> list[str]:
    return [part for part in re.split(r"[\\/]+", path) if part]


def summarize_manifest_files(files: list[ManifestFile]) -> tuple[int, int, int]:
    unique_hashes = {file.sha256.lower() for file in files}
    total_size = sum(int(file.size) for file in files)
    return len(files), len(unique_hashes), total_size


def count_manifest_directories(files: list[ManifestFile]) -> int:
    directories: set[str] = set()
    for file in files:
        parts = split_manifest_path(file.path)
        current: list[str] = []
        for segment in parts[:-1]:
            current.append(segment)
            directories.add("/".join(current))
    return len(directories)


def build_project_commit_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for _path, payload in iter_project_commit_files():
        project = payload.get("manifest", {}).get("project")
        if not project:
            continue
        counts[project] = counts.get(project, 0) + 1
    return counts


def build_project_pending_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for _path, payload in iter_pending_sync_files():
        project = payload.get("manifest", {}).get("project")
        if not project:
            continue
        counts[project] = counts.get(project, 0) + 1
    return counts


def iter_latest_manifest_files():
    for path in sorted(LATEST_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Skipping corrupted latest manifest: %s", path)
            continue
        yield path, payload


def read_log_tail(limit: int = 200) -> list[str]:
    if not LOG_FILE.exists():
        return []
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-limit:]


def load_commit_manifest(project: str, commit_id: str) -> SyncCheckRequest:
    path = MANIFESTS_DIR / f"{commit_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Commit '{commit_id}' not found")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Commit file is corrupted") from exc

    manifest = payload.get("manifest", {})
    if manifest.get("project") != project:
        raise HTTPException(status_code=404, detail=f"Commit '{commit_id}' does not belong to project '{project}'")

    return SyncCheckRequest(**manifest)


def load_latest_manifest_payload(project: str) -> dict:
    path = latest_manifest_path(project)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No manifest known for project '{project}'")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.exception("Latest manifest file is corrupted: %s", path)
        raise HTTPException(status_code=500, detail="Stored latest manifest is corrupted") from exc


def list_dashboard_projects() -> list[DashboardProjectSummary]:
    commit_counts = build_project_commit_counts()
    pending_counts = build_project_pending_counts()
    items: list[DashboardProjectSummary] = []

    for _path, payload in iter_latest_manifest_files():
        try:
            latest = LatestManifestResponse(**payload)
        except Exception:
            logger.warning("Skipping invalid latest manifest payload for dashboard: %s", payload)
            continue

        file_count, object_count, total_size = summarize_manifest_files(latest.files)
        pending_syncs = pending_counts.get(latest.project, 0)
        status = "Pending sync" if pending_syncs else "Available"
        items.append(
            DashboardProjectSummary(
                project=latest.project,
                updated_at=latest.updated_at,
                sender=latest.sender,
                commit_message=latest.commit_message,
                status=status,
                file_count=file_count,
                object_count=object_count,
                total_size=total_size,
                commit_count=commit_counts.get(latest.project, 0),
                pending_syncs=pending_syncs,
            )
        )

    items.sort(key=lambda item: item.updated_at, reverse=True)
    return items


def list_public_project_names() -> list[str]:
    return [item.project for item in list_dashboard_projects()]


def build_dashboard_project_detail(project: str) -> DashboardProjectDetail:
    payload = load_latest_manifest_payload(project)
    latest = LatestManifestResponse(**payload)
    manifest = build_manifest_from_latest_payload(payload)
    report = verify_manifest_integrity(project, manifest)
    file_count, object_count, total_size = summarize_manifest_files(latest.files)
    commits = list_project_commits(project)
    latest_commit_id = commits[0].commit_id if commits else None
    pending_syncs = build_project_pending_counts().get(project, 0)

    return DashboardProjectDetail(
        project=latest.project,
        updated_at=latest.updated_at,
        sender=latest.sender,
        commit_message=latest.commit_message,
        status=report.status,
        file_count=file_count,
        object_count=object_count,
        total_size=total_size,
        directory_count=count_manifest_directories(latest.files),
        commit_count=len(commits),
        pending_syncs=pending_syncs,
        missing_objects=report.missing_objects,
        corrupted_objects=report.corrupted_objects,
        checked_at=report.checked_at,
        latest_manifest_file=latest_manifest_path(project).name,
        latest_commit_id=latest_commit_id,
        files=latest.files,
        commits=commits,
    )


def build_connection_status_response() -> ConnectionStatus:
    logger.info("Building connection status response")
    return ConnectionStatus(
        connected=True,
        message="Server connection is available",
        server_time=datetime.now(timezone.utc),
    )


def build_dashboard_status() -> DashboardStatus:
    now = datetime.now(timezone.utc)
    return DashboardStatus(
        connected=True,
        message="RavenLib server is running",
        server_time=now,
        started_at=STARTED_AT,
        uptime_seconds=max(0, int((now - STARTED_AT).total_seconds())),
        projects=count_projects(),
        commits=sum(1 for _ in iter_project_commit_files()),
        objects=count_stored_objects(),
    )


def build_sync_check_response(
    manifest: SyncCheckRequest,
    sender: str,
    manifest_path: Path,
    received_at: datetime,
    need_upload: list[str],
    sync_id: str,
) -> SyncCheckResponse:
    logger.info(
        "Pending sync prepared: project=%s files=%s need_upload=%s sync_id=%s",
        manifest.project,
        len(manifest.files),
        len(need_upload),
        sync_id,
    )
    return SyncCheckResponse(
        accepted=True,
        message=(
            f"Manifest for project '{manifest.project}' received from {sender}. "
            f"Files: {len(manifest.files)}. Missing objects: {len(need_upload)}. "
            "Manifest will become latest only after sync finalization."
        ),
        received_at=received_at,
        sender=sender,
        manifest_file=manifest_path.name,
        commit_message=manifest.commit_message,
        need_upload=need_upload,
        sync_id=sync_id,
    )


def collect_all_referenced_hashes() -> set[str]:
    """Return hashes still referenced by any remaining latest, commit, or pending payload."""

    referenced: set[str] = set()

    for latest_path in sorted(LATEST_DIR.glob("*.json")):
        try:
            payload = json.loads(latest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Skipping corrupted latest manifest while collecting hashes: %s", latest_path)
            continue
        for file in payload.get("files", []):
            sha256 = str(file.get("sha256", "")).lower()
            if sha256:
                referenced.add(sha256)

    for _path, payload in iter_project_commit_files():
        for file in payload.get("manifest", {}).get("files", []):
            sha256 = str(file.get("sha256", "")).lower()
            if sha256:
                referenced.add(sha256)

    for _path, payload in iter_pending_sync_files():
        for file in payload.get("manifest", {}).get("files", []):
            sha256 = str(file.get("sha256", "")).lower()
            if sha256:
                referenced.add(sha256)

    return referenced


def prune_orphaned_objects() -> int:
    """Delete objects no longer referenced by any project metadata."""

    referenced_hashes = collect_all_referenced_hashes()
    removed_objects = 0

    for stored_object in OBJECTS_DIR.rglob("*"):
        if not stored_object.is_file():
            continue
        if stored_object.name.lower() in referenced_hashes:
            continue
        stored_object.unlink(missing_ok=True)
        removed_objects += 1
        logger.info("Removed orphaned object: %s", stored_object)

    for directory in sorted((path for path in OBJECTS_DIR.rglob("*") if path.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            continue

    return removed_objects


@app.get("/api/auth/status", response_model=AuthStatusResponse)
def get_auth_status() -> AuthStatusResponse:
    configured = is_server_password_configured()
    return AuthStatusResponse(
        password_configured=configured,
        requires_bootstrap=not configured,
        minimum_password_length=MIN_SERVER_PASSWORD_LENGTH,
        dashboard_session_ttl_hours=DASHBOARD_SESSION_TTL_HOURS,
        server_time=datetime.now(timezone.utc),
    )


@app.post("/api/auth/bootstrap", response_model=AuthLoginResponse)
def bootstrap_auth(payload: AuthBootstrapRequest) -> AuthLoginResponse:
    if is_server_password_configured():
        raise HTTPException(status_code=409, detail="Server password is already configured.")

    new_password = validate_server_password(payload.new_password)
    update_server_password(new_password)
    session_token, expires_at = create_dashboard_session()
    logger.info("Server password configured from dashboard bootstrap.")
    return AuthLoginResponse(
        authenticated=True,
        message="Server password configured successfully.",
        session_token=session_token,
        expires_at=expires_at,
    )


@app.post("/api/auth/login", response_model=AuthLoginResponse)
def login_auth(payload: AuthLoginRequest) -> AuthLoginResponse:
    if not is_server_password_configured():
        raise HTTPException(
            status_code=503,
            detail="Server password is not configured yet. Open the dashboard and set it first.",
        )

    if not verify_server_password(payload.password):
        raise HTTPException(status_code=401, detail="Incorrect password.")

    session_token, expires_at = create_dashboard_session()
    logger.info("Dashboard login succeeded.")
    return AuthLoginResponse(
        authenticated=True,
        message="Authentication successful.",
        session_token=session_token,
        expires_at=expires_at,
    )


@app.post("/api/auth/change-password", response_model=PasswordChangeResponse)
def change_auth_password(payload: PasswordChangeRequest) -> PasswordChangeResponse:
    if not is_server_password_configured():
        raise HTTPException(status_code=503, detail="Server password is not configured yet.")

    if not verify_server_password(payload.current_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")

    new_password = validate_server_password(payload.new_password)
    if normalize_server_password(payload.current_password) == new_password:
        raise HTTPException(status_code=400, detail="New password must be different from the current password.")

    update_server_password(new_password)
    session_token, expires_at = create_dashboard_session()
    logger.info("Server password changed from dashboard.")
    return PasswordChangeResponse(
        changed=True,
        message="Server password updated successfully.",
        session_token=session_token,
        expires_at=expires_at,
    )


@app.get("/api/connection/status", response_model=ConnectionStatus)
def get_connection_status() -> ConnectionStatus:
    return build_connection_status_response()


@app.get("/api/projects", response_model=ProjectNameList)
def get_project_names() -> ProjectNameList:
    return ProjectNameList(items=list_public_project_names())


@app.get("/")
def root() -> FileResponse:
    logger.info("Serving server dashboard")
    return FileResponse(SERVER_WEB_DIR / "index.html")


@app.get("/auth")
@app.get("/login")
def auth_page() -> FileResponse:
    logger.info("Serving server auth page")
    return FileResponse(SERVER_WEB_DIR / "auth.html")


@app.get("/styles.css")
def get_dashboard_styles() -> FileResponse:
    return FileResponse(SERVER_WEB_DIR / "styles.css", media_type="text/css")


@app.get("/app.js")
def get_dashboard_script() -> FileResponse:
    return FileResponse(SERVER_WEB_DIR / "app.js", media_type="application/javascript")


@app.get("/auth.js")
def get_auth_script() -> FileResponse:
    return FileResponse(SERVER_WEB_DIR / "auth.js", media_type="application/javascript")


@app.get("/api/dashboard/status", response_model=DashboardStatus)
def get_dashboard_status() -> DashboardStatus:
    return build_dashboard_status()


@app.get("/api/dashboard/projects", response_model=DashboardProjectList)
def get_dashboard_projects() -> DashboardProjectList:
    return DashboardProjectList(items=list_dashboard_projects())


@app.get("/api/dashboard/projects/{project}", response_model=DashboardProjectDetail)
def get_dashboard_project(project: str) -> DashboardProjectDetail:
    return build_dashboard_project_detail(project)


@app.get("/api/dashboard/commits", response_model=DashboardCommitList)
def get_dashboard_commits(limit: int = 50) -> DashboardCommitList:
    safe_limit = max(1, min(limit, 200))
    return DashboardCommitList(items=list_recent_commits(safe_limit))


@app.get("/api/dashboard/logs", response_model=DashboardLogs)
def get_dashboard_logs(limit: int = 200) -> DashboardLogs:
    safe_limit = max(1, min(limit, 1000))
    return DashboardLogs(lines=read_log_tail(safe_limit))


@app.post("/sync/check", response_model=SyncCheckResponse)
def check_sync_manifest(request: Request, manifest: SyncCheckRequest) -> SyncCheckResponse:
    """Stage a sync request without publishing it as latest yet."""

    received_at = datetime.now(timezone.utc)
    sender = get_sender_identifier(request)
    need_upload = sorted({file.sha256.lower() for file in manifest.files if not object_path(file.sha256).exists()})

    with STATE_LOCK:
        sync_id, manifest_path = create_pending_sync(manifest, sender, received_at, need_upload)

    return build_sync_check_response(manifest, sender, manifest_path, received_at, need_upload, sync_id)


@app.post("/sync/finalize/{sync_id}", response_model=SyncFinalizeResponse)
def finalize_sync(sync_id: str) -> SyncFinalizeResponse:
    """Publish latest only after every referenced object is present on disk."""

    with STATE_LOCK:
        payload = load_pending_sync_payload(sync_id)
        manifest = SyncCheckRequest(**payload["manifest"])
        sender = payload.get("sender", "unknown-client")
        received_at = datetime.now(timezone.utc)

        report = ensure_manifest_objects_present(manifest.project, manifest, "Latest manifest")
        manifest_path = commit_manifest_as_latest(manifest, sender, received_at)
        delete_pending_sync(sync_id)

    logger.info(
        "Sync finalized: sync_id=%s project=%s uploaded=%s objects=%s",
        sync_id,
        manifest.project,
        len(payload.get("uploaded_hashes", [])),
        report.object_count,
    )
    return SyncFinalizeResponse(
        accepted=True,
        message=(
            f"Sync finalized for project '{manifest.project}'. "
            f"Latest manifest published with {report.object_count} object(s)."
        ),
        received_at=received_at,
        sender=sender,
        manifest_file=manifest_path.name,
        commit_message=manifest.commit_message,
        uploaded_objects=len(payload.get("uploaded_hashes", [])),
        object_count=report.object_count,
        sync_id=sync_id,
    )


@app.get("/sync/manifest/{project}", response_model=LatestManifestResponse)
def get_latest_manifest(project: str) -> LatestManifestResponse:
    """Return the current manifest only if every referenced object is available."""

    payload = load_latest_manifest_payload(project)
    manifest = build_manifest_from_latest_payload(payload)
    ensure_manifest_objects_present(project, manifest, "Latest manifest")
    return LatestManifestResponse(**payload)


@app.get("/sync/verify/{project}", response_model=ProjectIntegrityResponse)
def verify_project_integrity(project: str) -> ProjectIntegrityResponse:
    """Verify the current project manifest against stored objects."""

    payload = load_latest_manifest_payload(project)
    manifest = build_manifest_from_latest_payload(payload)
    report = verify_manifest_integrity(project, manifest)
    logger.info(
        "Integrity verified: project=%s status=%s missing=%s corrupted=%s",
        project,
        report.status,
        report.missing_objects,
        report.corrupted_objects,
    )
    return report


@app.post("/sync/reset/{project}", response_model=ProjectResetResponse)
def reset_project(project: str) -> ProjectResetResponse:
    """Delete all project metadata and prune orphaned objects."""

    checked_at = datetime.now(timezone.utc)
    removed_commits = 0
    removed_pending_syncs = 0
    removed_latest_manifest = False

    with STATE_LOCK:
        commit_paths = []
        for path, payload in iter_project_commit_files():
            manifest = payload.get("manifest", {})
            if manifest.get("project") == project:
                commit_paths.append(path)

        pending_paths = []
        for path, payload in iter_pending_sync_files():
            manifest = payload.get("manifest", {})
            if manifest.get("project") == project:
                pending_paths.append(path)

        latest_path = latest_manifest_path(project)
        if not commit_paths and not pending_paths and not latest_path.exists():
            raise HTTPException(status_code=404, detail=f"Project '{project}' does not exist on server")

        for path in commit_paths:
            path.unlink(missing_ok=True)
            removed_commits += 1

        for path in pending_paths:
            path.unlink(missing_ok=True)
            removed_pending_syncs += 1

        if latest_path.exists():
            latest_path.unlink(missing_ok=True)
            removed_latest_manifest = True

        removed_objects = prune_orphaned_objects()

    logger.info(
        "Project reset completed: project=%s commits=%s pending=%s latest=%s objects=%s",
        project,
        removed_commits,
        removed_pending_syncs,
        removed_latest_manifest,
        removed_objects,
    )
    return ProjectResetResponse(
        project=project,
        removed_commits=removed_commits,
        removed_pending_syncs=removed_pending_syncs,
        removed_latest_manifest=removed_latest_manifest,
        removed_objects=removed_objects,
        checked_at=checked_at,
        message=f"Project '{project}' has been permanently removed from the server.",
    )


@app.get("/sync/commits/{project}", response_model=list[CommitSummary])
def get_project_commits(project: str) -> list[CommitSummary]:
    return list_project_commits(project)


@app.get("/sync/commits/{project}/{commit_id}", response_model=SyncCheckRequest)
def get_project_commit(project: str, commit_id: str) -> SyncCheckRequest:
    return load_commit_manifest(project, commit_id)


@app.post("/sync/commits/{project}/{commit_id}/rollback", response_model=SyncFinalizeResponse)
def rollback_project_to_commit(project: str, commit_id: str) -> SyncFinalizeResponse:
    """Create a new latest state from a historical manifest only if its objects are present."""

    with STATE_LOCK:
        manifest = load_commit_manifest(project, commit_id)
        report = ensure_manifest_objects_present(project, manifest, f"Rollback target '{commit_id}'")

        received_at = datetime.now(timezone.utc)
        sender = f"rollback:{commit_id}"
        original_message = manifest.commit_message.strip()
        rollback_message = f"Rollback to {commit_id}"
        if original_message:
            rollback_message = f"{rollback_message}: {original_message}"
        manifest = manifest.model_copy(update={"commit_message": rollback_message})

        manifest_path = commit_manifest_as_latest(
            manifest,
            sender,
            received_at,
            is_rollback=True,
            reverted_from=commit_id,
        )

    logger.info("Project '%s' rolled back to commit '%s'", project, commit_id)
    return SyncFinalizeResponse(
        accepted=True,
        message=f"Project '{project}' rolled back to commit '{commit_id}'.",
        received_at=received_at,
        sender=sender,
        manifest_file=manifest_path.name,
        commit_message=manifest.commit_message,
        uploaded_objects=0,
        object_count=report.object_count,
        sync_id=None,
    )


@app.put("/objects/{sha256}", response_model=ObjectUploadResponse)
async def upload_object(
    sha256: str,
    request: Request,
    sync_id: str | None = Query(default=None),
) -> ObjectUploadResponse:
    """Store one object blob and optionally attach it to a pending sync."""

    normalized_sha256 = sha256.lower()
    if sync_id:
        payload = load_pending_sync_payload(sync_id)
        requested_hashes = set(payload.get("need_upload", []))
        if normalized_sha256 not in requested_hashes:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Object '{normalized_sha256}' was not requested for pending sync '{sync_id}'. "
                    "Use the server's need_upload list only."
                ),
            )

    path = object_path(normalized_sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{normalized_sha256}.{uuid.uuid4().hex}.uploading")
    digest = hashlib.sha256()
    total_size = 0
    chunk_size = get_stream_chunk_size_bytes()

    logger.info(
        "Streaming upload started: sha256=%s sync_id=%s chunk_size=%s",
        normalized_sha256,
        sync_id,
        chunk_size,
    )

    try:
        with temp_path.open("wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                handle.write(chunk)
                digest.update(chunk)
                total_size += len(chunk)
                logger.info(
                    "Chunk received: sha256=%s bytes=%s total=%s",
                    normalized_sha256,
                    len(chunk),
                    total_size,
                )
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        logger.exception("Streaming upload failed while writing object: sha256=%s", normalized_sha256)
        raise HTTPException(status_code=500, detail=f"Failed to store object '{normalized_sha256}'.") from exc

    computed = digest.hexdigest()
    if computed != normalized_sha256:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"Uploaded content hash ({computed}) does not match sha256 in URL ({sha256})",
        )

    logger.info("SHA verified: sha256=%s size=%s", normalized_sha256, total_size)

    if path.exists():
        actual_sha256 = compute_file_sha256(path)
        if actual_sha256 == normalized_sha256:
            temp_path.unlink(missing_ok=True)
            logger.info("Object already present, skipping replace: sha256=%s", normalized_sha256)
        else:
            os.replace(temp_path, path)
            logger.warning(
                "Existing object replaced after integrity mismatch: sha256=%s previous_sha256=%s",
                normalized_sha256,
                actual_sha256,
            )
    else:
        os.replace(temp_path, path)
        logger.info("Object stored: sha256=%s size=%s", normalized_sha256, total_size)

    if sync_id:
        with STATE_LOCK:
            update_pending_uploaded_hash(sync_id, normalized_sha256)

    logger.info("Upload completed: sha256=%s size=%s", normalized_sha256, total_size)
    return ObjectUploadResponse(sha256=normalized_sha256, stored=True, size=total_size, sync_id=sync_id)


@app.get("/objects/{sha256}")
def download_object(sha256: str) -> Response:
    """Return raw object bytes by hash."""

    normalized_sha256 = sha256.lower()
    path = object_path(normalized_sha256)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Object '{normalized_sha256}' is missing on server. Verify Server Integrity to inspect the project.",
        )

    actual_sha256 = compute_file_sha256(path)
    if actual_sha256 != normalized_sha256:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Object '{normalized_sha256}' is corrupted on server "
                f"(actual SHA256: {actual_sha256}). Verify Server Integrity for details."
            ),
        )

    chunk_size = get_stream_chunk_size_bytes()
    file_size = path.stat().st_size
    logger.info(
        "Streaming download started: sha256=%s size=%s chunk_size=%s",
        normalized_sha256,
        file_size,
        chunk_size,
    )
    logger.info("SHA verified: sha256=%s size=%s", normalized_sha256, file_size)

    def stream_file() -> object:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                logger.info(
                    "Chunk written: sha256=%s bytes=%s",
                    normalized_sha256,
                    len(chunk),
                )
                yield chunk
        logger.info("Download completed: sha256=%s size=%s", normalized_sha256, file_size)

    return StreamingResponse(
        stream_file(),
        media_type="application/octet-stream",
        headers={"Content-Length": str(file_size)},
    )

