const REFRESH_INTERVAL_MS = 10000;
const SESSION_STORAGE_KEY = "ravenlib-dashboard-session";

const state = {
    theme: localStorage.getItem("ravenlib-server-theme") || "light",
    refreshHandle: null,
    projects: [],
    selectedProject: null,
    projectDetail: null,
    projectRequestId: 0,
    sessionToken: localStorage.getItem(SESSION_STORAGE_KEY) || "",
    authMode: "login",
    authStatus: null,
};

function buildAuthRedirectUrl() {
    const next = `${window.location.pathname}${window.location.search}${window.location.hash}` || "/";
    return `/auth?next=${encodeURIComponent(next)}`;
}

function redirectToAuthPage() {
    window.location.replace(buildAuthRedirectUrl());
}

const elements = {
    authOverlay: document.getElementById("authOverlay"),
    authForm: document.getElementById("authForm"),
    authTitle: document.getElementById("authTitle"),
    authSubtitle: document.getElementById("authSubtitle"),
    authPassword: document.getElementById("authPassword"),
    authConfirmRow: document.getElementById("authConfirmRow"),
    authConfirmPassword: document.getElementById("authConfirmPassword"),
    authMessage: document.getElementById("authMessage"),
    authSubmitButton: document.getElementById("authSubmitButton"),
    sessionState: document.getElementById("sessionState"),
    logoutButton: document.getElementById("logoutButton"),
    passwordUpdatedMeta: document.getElementById("passwordUpdatedMeta"),
    changePasswordForm: document.getElementById("changePasswordForm"),
    currentPassword: document.getElementById("currentPassword"),
    newPassword: document.getElementById("newPassword"),
    confirmNewPassword: document.getElementById("confirmNewPassword"),
    changePasswordResult: document.getElementById("changePasswordResult"),
    serverHealth: document.getElementById("serverHealth"),
    refreshButton: document.getElementById("refreshButton"),
    themeToggleButton: document.getElementById("themeToggleButton"),
    themeToggleLabel: document.getElementById("themeToggleLabel"),
    statusTimestamp: document.getElementById("statusTimestamp"),
    statusCards: document.getElementById("statusCards"),
    historyMeta: document.getElementById("historyMeta"),
    historyTableBody: document.getElementById("historyTableBody"),
    historyRowTemplate: document.getElementById("historyRowTemplate"),
    logMeta: document.getElementById("logMeta"),
    logOutput: document.getElementById("logOutput"),
    projectsMeta: document.getElementById("projectsMeta"),
    projectsList: document.getElementById("projectsList"),
    projectCardTemplate: document.getElementById("projectCardTemplate"),
    projectTitle: document.getElementById("projectTitle"),
    projectSubtitle: document.getElementById("projectSubtitle"),
    projectStatusBadge: document.getElementById("projectStatusBadge"),
    refreshProjectButton: document.getElementById("refreshProjectButton"),
    projectPreviewEmpty: document.getElementById("projectPreviewEmpty"),
    projectPreview: document.getElementById("projectPreview"),
    projectCheckedAt: document.getElementById("projectCheckedAt"),
    projectSummaryCards: document.getElementById("projectSummaryCards"),
    manifestMeta: document.getElementById("manifestMeta"),
    manifestTree: document.getElementById("manifestTree"),
    projectHistoryMeta: document.getElementById("projectHistoryMeta"),
    projectHistoryBody: document.getElementById("projectHistoryBody"),
    projectCommitRowTemplate: document.getElementById("projectCommitRowTemplate"),
    deleteResult: document.getElementById("deleteResult"),
    deleteProjectButton: document.getElementById("deleteProjectButton"),
};

async function apiRequest(path, options = {}) {
    const headers = {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
    };
    if (state.sessionToken) {
        headers.Authorization = `Bearer ${state.sessionToken}`;
    }

    const response = await fetch(path, {
        method: options.method || "GET",
        headers,
        body: options.body ? JSON.stringify(options.body) : undefined,
    });
    const text = await response.text();
    let payload = {};
    if (text) {
        try {
            payload = JSON.parse(text);
        } catch (_error) {
            payload = { detail: text };
        }
    }
    if (!response.ok) {
        const error = new Error(payload.detail || payload.error || `Request failed with ${response.status}`);
        error.status = response.status;
        throw error;
    }
    return payload;
}

async function getAuthStatus() {
    const payload = await apiRequest("/api/auth/status");
    state.authStatus = payload;
    return payload;
}

function persistSessionToken(token) {
    state.sessionToken = token || "";
    if (state.sessionToken) {
        localStorage.setItem(SESSION_STORAGE_KEY, state.sessionToken);
    } else {
        localStorage.removeItem(SESSION_STORAGE_KEY);
    }
    renderSessionState();
}

function renderSessionState() {
    const unlocked = Boolean(state.sessionToken);
    elements.sessionState.textContent = unlocked ? "Unlocked" : "Locked";
    elements.sessionState.className = `status-badge ${unlocked ? "status-online" : "status-idle"}`;
}

function setAuthMessage(message, mode = "info") {
    elements.authMessage.textContent = message || "";
    elements.authMessage.className = `auth-message ${message ? `danger-result-${mode}` : ""}`.trim();
}

function setChangePasswordMessage(message, mode = "info") {
    elements.changePasswordResult.textContent = message || "";
    elements.changePasswordResult.className = `danger-result ${message ? `danger-result-${mode}` : ""}`.trim();
}

function renderAuthOverlay() {
    const bootstrap = state.authMode === "bootstrap";
    elements.authTitle.textContent = bootstrap ? "Set Server Password" : "Server Login";
    elements.authSubtitle.textContent = bootstrap
        ? "Create the shared server password. This password will protect sync requests, the dashboard, and client access."
        : "Enter the shared server password to unlock the dashboard and protected sync API.";
    elements.authConfirmRow.classList.toggle("hidden", !bootstrap);
    elements.authConfirmPassword.required = bootstrap;
    elements.authSubmitButton.textContent = bootstrap ? "Save Password" : "Unlock Dashboard";
    elements.authOverlay.classList.remove("hidden");
    document.body.classList.add("dashboard-locked");
    renderSessionState();
}

function hideAuthOverlay() {
    elements.authOverlay.classList.add("hidden");
    document.body.classList.remove("dashboard-locked");
    elements.authPassword.value = "";
    elements.authConfirmPassword.value = "";
    setAuthMessage("");
    renderSessionState();
}

function lockDashboard(message = "") {
    clearTimeout(state.refreshHandle);
    persistSessionToken("");
    if (message) {
        console.warn(message);
    }
    redirectToAuthPage();
}

async function ensureAuthenticated() {
    const authStatus = await getAuthStatus();
    state.authMode = authStatus.requires_bootstrap ? "bootstrap" : "login";
    if (!state.sessionToken) {
        redirectToAuthPage();
        return false;
    }

    try {
        await apiRequest("/api/dashboard/status");
        hideAuthOverlay();
        return true;
    } catch (error) {
        if (error.status === 401 || error.status === 503) {
            lockDashboard(error.message || "Authentication required.");
            return false;
        }
        throw error;
    }
}

async function submitAuth(event) {
    event.preventDefault();

    const authStatus = state.authStatus || (await getAuthStatus());
    const password = elements.authPassword.value.trim();
    const confirm = elements.authConfirmPassword.value.trim();

    if (!password) {
        setAuthMessage("Password is required.", "error");
        return;
    }

    if (authStatus.requires_bootstrap) {
        if (password.length < authStatus.minimum_password_length) {
            setAuthMessage(
                `Password must be at least ${authStatus.minimum_password_length} characters long.`,
                "error",
            );
            return;
        }
        if (password !== confirm) {
            setAuthMessage("Password confirmation does not match.", "error");
            return;
        }
    }

    elements.authSubmitButton.disabled = true;
    setAuthMessage(authStatus.requires_bootstrap ? "Saving password..." : "Checking password...");

    try {
        const endpoint = authStatus.requires_bootstrap ? "/api/auth/bootstrap" : "/api/auth/login";
        const body = authStatus.requires_bootstrap ? { new_password: password } : { password };
        const payload = await apiRequest(endpoint, { method: "POST", body });
        persistSessionToken(payload.session_token || "");
        state.authMode = "login";
        hideAuthOverlay();
        await refreshDashboard({ refreshSelectedProject: true });
    } catch (error) {
        setAuthMessage(error.message || String(error), "error");
    } finally {
        elements.authSubmitButton.disabled = false;
    }
}

async function handleChangePassword(event) {
    event.preventDefault();

    const currentPassword = elements.currentPassword.value.trim();
    const newPassword = elements.newPassword.value.trim();
    const confirmPassword = elements.confirmNewPassword.value.trim();
    const minimumLength = state.authStatus?.minimum_password_length || 8;

    if (!currentPassword || !newPassword || !confirmPassword) {
        setChangePasswordMessage("Fill in all password fields.", "error");
        return;
    }
    if (newPassword.length < minimumLength) {
        setChangePasswordMessage(`New password must be at least ${minimumLength} characters long.`, "error");
        return;
    }
    if (newPassword !== confirmPassword) {
        setChangePasswordMessage("New password confirmation does not match.", "error");
        return;
    }

    setChangePasswordMessage("Updating password...");

    try {
        const payload = await apiRequest("/api/auth/change-password", {
            method: "POST",
            body: {
                current_password: currentPassword,
                new_password: newPassword,
            },
        });
        persistSessionToken(payload.session_token || "");
        elements.changePasswordForm.reset();
        setChangePasswordMessage(payload.message || "Password updated.", "success");
        elements.passwordUpdatedMeta.textContent = `Session renewed until ${formatDate(payload.expires_at)}`;
    } catch (error) {
        if (error.status === 401 || error.status === 503) {
            lockDashboard(error.message || "Session expired.");
            return;
        }
        setChangePasswordMessage(error.message || String(error), "error");
    }
}

function formatDate(value) {
    if (!value) {
        return "Unknown";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
        return value;
    }
    return date.toLocaleString("ru-RU", { hour12: false });
}

function formatDuration(totalSeconds) {
    const seconds = Math.max(0, Number(totalSeconds || 0));
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const rest = seconds % 60;
    const parts = [];
    if (days) {
        parts.push(`${days}d`);
    }
    if (hours || parts.length) {
        parts.push(`${hours}h`);
    }
    if (minutes || parts.length) {
        parts.push(`${minutes}m`);
    }
    parts.push(`${rest}s`);
    return parts.join(" ");
}

function formatBytes(value) {
    const size = Number(value || 0);
    if (!Number.isFinite(size) || size <= 0) {
        return "0 B";
    }
    const units = ["B", "KB", "MB", "GB", "TB"];
    const exponent = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
    const amount = size / 1024 ** exponent;
    const digits = amount >= 100 || exponent === 0 ? 0 : amount >= 10 ? 1 : 2;
    return `${amount.toFixed(digits)} ${units[exponent]}`;
}

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

function abbreviateCommitId(value) {
    const text = String(value || "");
    return text.length > 16 ? `${text.slice(0, 16)}...` : text;
}

function normalizePath(path) {
    return String(path || "").replaceAll("\\", "/");
}

function statusClass(status) {
    const normalized = String(status || "").toLowerCase();
    if (normalized.includes("healthy") || normalized.includes("available") || normalized.includes("online")) {
        return "status-online";
    }
    if (normalized.includes("pending")) {
        return "status-pending";
    }
    if (normalized.includes("corrupted") || normalized.includes("offline") || normalized.includes("error")) {
        return "status-offline";
    }
    return "status-idle";
}

function applyTheme(theme) {
    state.theme = theme === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = state.theme;
    localStorage.setItem("ravenlib-server-theme", state.theme);
    elements.themeToggleLabel.textContent = state.theme === "dark" ? "Light theme" : "Dark theme";
}

function toggleTheme() {
    applyTheme(state.theme === "dark" ? "light" : "dark");
}

function scheduleRefresh() {
    clearTimeout(state.refreshHandle);
    state.refreshHandle = setTimeout(() => {
        refreshDashboard({ refreshSelectedProject: true });
    }, REFRESH_INTERVAL_MS);
}

function readProjectHash() {
    const match = window.location.hash.match(/^#project=(.+)$/);
    if (!match) {
        return null;
    }
    try {
        return decodeURIComponent(match[1]);
    } catch (_error) {
        return null;
    }
}

function writeProjectHash(project) {
    const nextHash = project ? `#project=${encodeURIComponent(project)}` : "";
    if (window.location.hash === nextHash) {
        return;
    }
    if (!nextHash) {
        history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
        return;
    }
    window.location.hash = nextHash;
}

function showDeleteResult(message, mode = "info") {
    elements.deleteResult.textContent = message || "";
    elements.deleteResult.className = `danger-result ${message ? `danger-result-${mode}` : ""}`.trim();
}

function renderStatus(status) {
    elements.serverHealth.textContent = status.connected ? "Online" : "Offline";
    elements.serverHealth.className = `status-badge ${status.connected ? "status-online" : "status-offline"}`;
    elements.statusTimestamp.textContent = `Updated: ${formatDate(status.server_time)}`;

    const cards = [
        ["Message", status.message || "No message"],
        ["Server Time", formatDate(status.server_time)],
        ["Started At", formatDate(status.started_at)],
        ["Uptime", formatDuration(status.uptime_seconds)],
        ["Projects", String(status.projects ?? 0)],
        ["Commits", String(status.commits ?? 0)],
        ["Objects", String(status.objects ?? 0)],
    ];

    elements.statusCards.innerHTML = cards
        .map(
            ([label, value]) => `
                <article class="metric-card">
                    <span class="metric-label">${escapeHtml(label)}</span>
                    <strong class="metric-value">${escapeHtml(value)}</strong>
                </article>
            `,
        )
        .join("");
}

function renderHistory(items) {
    elements.historyMeta.textContent = `${items.length} item(s)`;
    if (!items.length) {
        elements.historyTableBody.innerHTML =
            '<tr><td colspan="6" class="history-placeholder">No commits recorded yet.</td></tr>';
        return;
    }

    elements.historyTableBody.innerHTML = "";
    for (const item of items) {
        const row = elements.historyRowTemplate.content.firstElementChild.cloneNode(true);
        row.querySelector('[data-field="received_at"]').textContent = formatDate(item.received_at);
        row.querySelector('[data-field="project"]').textContent = item.project || "Unknown";
        row.querySelector('[data-field="sender"]').textContent = item.sender || "Unknown";
        row.querySelector('[data-field="files"]').textContent = String(item.files ?? 0);
        row.querySelector('[data-field="type"]').textContent = item.is_rollback ? "Rollback" : "Sync";
        row.querySelector('[data-field="commit_message"]').textContent = item.commit_message || "-";
        if (item.project) {
            row.classList.add("clickable-row");
            row.addEventListener("click", () => {
                selectProject(item.project).catch((error) => {
                    renderFailure(error);
                });
            });
        }
        elements.historyTableBody.appendChild(row);
    }
}

function renderLogs(payload) {
    const lines = payload.lines || [];
    elements.logMeta.textContent = `${lines.length} line(s)`;
    elements.logOutput.textContent = lines.length ? lines.join("\n") : "No log lines available.";
}

function renderFailure(error) {
    const message = error?.message || String(error);
    elements.serverHealth.textContent = "Error";
    elements.serverHealth.className = "status-badge status-offline";
    elements.statusTimestamp.textContent = message;
    elements.projectsMeta.textContent = "Load failed";
    if (!state.projectDetail) {
        renderProjectEmpty(message);
    }
}

function renderProjectsList(items) {
    elements.projectsMeta.textContent = `${items.length} project(s)`;
    if (!items.length) {
        elements.projectsList.innerHTML = '<div class="empty-state">No projects stored on the server yet.</div>';
        return;
    }

    elements.projectsList.innerHTML = "";
    for (const item of items) {
        const card = elements.projectCardTemplate.content.firstElementChild.cloneNode(true);
        card.dataset.project = item.project;
        card.querySelector('[data-field="project"]').textContent = item.project;
        card.querySelector('[data-field="status"]').textContent = item.status || "Unknown";
        card.querySelector('[data-field="status"]').className = `mini-badge ${statusClass(item.status)}`;
        card.querySelector('[data-field="updated_at"]').textContent = `Updated: ${formatDate(item.updated_at)}`;
        card.querySelector('[data-field="commit_message"]').textContent = item.commit_message || "No commit message";
        card.querySelector('[data-field="stats"]').textContent =
            `${item.file_count} file(s) · ${item.commit_count} commit(s) · ${formatBytes(item.total_size)}`;
        if (item.project === state.selectedProject) {
            card.classList.add("is-active");
        }
        card.addEventListener("click", () => {
            selectProject(item.project).catch((error) => {
                renderFailure(error);
            });
        });
        elements.projectsList.appendChild(card);
    }
}

function renderProjectEmpty(message = "No project selected yet.") {
    elements.projectTitle.textContent = "Select a project";
    elements.projectSubtitle.textContent = message;
    elements.projectStatusBadge.textContent = "Waiting";
    elements.projectStatusBadge.className = "status-badge status-idle";
    elements.projectPreviewEmpty.textContent = message;
    elements.projectPreviewEmpty.classList.remove("hidden");
    elements.projectPreview.classList.add("hidden");
    elements.refreshProjectButton.disabled = !state.projects.length;
    elements.deleteProjectButton.disabled = true;
    showDeleteResult("");
}

function renderProjectLoading(project) {
    elements.projectTitle.textContent = project;
    elements.projectSubtitle.textContent = "Loading manifest preview, project metadata, and commit history.";
    elements.projectStatusBadge.textContent = "Loading";
    elements.projectStatusBadge.className = "status-badge status-idle";
    elements.projectPreviewEmpty.textContent = "Project preview is loading.";
    elements.projectPreviewEmpty.classList.remove("hidden");
    elements.projectPreview.classList.add("hidden");
    elements.refreshProjectButton.disabled = false;
    elements.deleteProjectButton.disabled = true;
}

function renderProjectError(project, error) {
    const message = error?.message || String(error);
    elements.projectTitle.textContent = project;
    elements.projectSubtitle.textContent = message;
    elements.projectStatusBadge.textContent = "Error";
    elements.projectStatusBadge.className = "status-badge status-offline";
    elements.projectPreviewEmpty.textContent = message;
    elements.projectPreviewEmpty.classList.remove("hidden");
    elements.projectPreview.classList.add("hidden");
    elements.deleteProjectButton.disabled = true;
}

function buildSummaryCards(detail) {
    const cards = [
        ["Last Update", formatDate(detail.updated_at)],
        ["Checked At", formatDate(detail.checked_at)],
        ["Sender", detail.sender || "Unknown"],
        ["Latest Commit", detail.latest_commit_id ? abbreviateCommitId(detail.latest_commit_id) : "Not recorded"],
        ["Manifest File", detail.latest_manifest_file || "Unknown"],
        ["Files", String(detail.file_count ?? 0)],
        ["Folders", String(detail.directory_count ?? 0)],
        ["Unique Objects", String(detail.object_count ?? 0)],
        ["Total Size", formatBytes(detail.total_size)],
        ["Commits", String(detail.commit_count ?? 0)],
        ["Pending Syncs", String(detail.pending_syncs ?? 0)],
        ["Missing Objects", String(detail.missing_objects ?? 0)],
        ["Corrupted Objects", String(detail.corrupted_objects ?? 0)],
        ["Latest Message", detail.commit_message || "No commit message"],
    ];

    return cards
        .map(
            ([label, value]) => `
                <article class="metric-card">
                    <span class="metric-label">${escapeHtml(label)}</span>
                    <strong class="metric-value">${escapeHtml(value)}</strong>
                </article>
            `,
        )
        .join("");
}

function buildManifestTree(files) {
    const root = { folders: new Map(), files: [] };

    for (const file of files) {
        const normalizedPath = normalizePath(file.path);
        const parts = normalizedPath.split("/").filter(Boolean);
        if (!parts.length) {
            continue;
        }

        let cursor = root;
        let currentPath = "";
        for (const segment of parts.slice(0, -1)) {
            currentPath = currentPath ? `${currentPath}/${segment}` : segment;
            if (!cursor.folders.has(segment)) {
                cursor.folders.set(segment, {
                    name: segment,
                    path: currentPath,
                    folders: new Map(),
                    files: [],
                });
            }
            cursor = cursor.folders.get(segment);
        }

        cursor.files.push({
            ...file,
            name: parts[parts.length - 1],
            path: normalizedPath,
        });
    }

    const lines = [];

    function walk(node, depth) {
        const folders = [...node.folders.values()].sort((left, right) => left.name.localeCompare(right.name, "ru"));
        for (const folder of folders) {
            lines.push({
                type: "dir",
                depth,
                name: folder.name,
                path: folder.path,
            });
            walk(folder, depth + 1);
        }

        const filesAtLevel = [...node.files].sort((left, right) => left.name.localeCompare(right.name, "ru"));
        for (const file of filesAtLevel) {
            lines.push({
                type: "file",
                depth,
                name: file.name,
                path: file.path,
                size: file.size,
                sha256: file.sha256,
            });
        }
    }

    walk(root, 0);
    return lines;
}

function renderManifestTree(detail) {
    const files = detail.files || [];
    elements.manifestMeta.textContent =
        `${detail.file_count} file(s) · ${detail.directory_count} folder(s) · ${formatBytes(detail.total_size)}`;

    if (!files.length) {
        elements.manifestTree.innerHTML = '<div class="empty-state">This manifest does not reference any files.</div>';
        return;
    }

    const lines = buildManifestTree(files);
    elements.manifestTree.innerHTML = lines
        .map((line) => {
            const meta =
                line.type === "dir"
                    ? line.path
                    : `${line.path} | ${formatBytes(line.size)} | ${String(line.sha256 || "").slice(0, 12)}`;
            const icon = line.type === "dir" ? "Folder" : "File";
            return `
                <div class="tree-row tree-row-${line.type}" style="--indent:${line.depth * 20}px">
                    <span class="tree-label">
                        <span class="tree-kind">${icon}</span>
                        <strong>${escapeHtml(line.name)}</strong>
                    </span>
                    <span class="tree-meta">${escapeHtml(meta)}</span>
                </div>
            `;
        })
        .join("");
}

function renderProjectHistory(commits) {
    elements.projectHistoryMeta.textContent = `${commits.length} item(s)`;
    if (!commits.length) {
        elements.projectHistoryBody.innerHTML =
            '<tr><td colspan="6" class="history-placeholder">No commits recorded for this project yet.</td></tr>';
        return;
    }

    elements.projectHistoryBody.innerHTML = "";
    for (const item of commits) {
        const row = elements.projectCommitRowTemplate.content.firstElementChild.cloneNode(true);
        row.querySelector('[data-field="received_at"]').textContent = formatDate(item.received_at);
        row.querySelector('[data-field="commit_id"]').textContent = abbreviateCommitId(item.commit_id);
        row.querySelector('[data-field="commit_id"]').title = item.commit_id || "";
        row.querySelector('[data-field="sender"]').textContent = item.sender || "Unknown";
        row.querySelector('[data-field="files"]').textContent = String(item.files ?? 0);
        row.querySelector('[data-field="type"]').textContent = item.is_rollback ? "Rollback" : "Sync";
        row.querySelector('[data-field="commit_message"]').textContent = item.commit_message || "-";
        elements.projectHistoryBody.appendChild(row);
    }
}

function renderProjectDetail(detail) {
    state.projectDetail = detail;
    state.selectedProject = detail.project;

    elements.projectTitle.textContent = detail.project;
    elements.projectSubtitle.textContent =
        `Latest manifest from ${detail.sender || "Unknown"} on ${formatDate(detail.updated_at)}.`;
    elements.projectStatusBadge.textContent = detail.status || "Unknown";
    elements.projectStatusBadge.className = `status-badge ${statusClass(detail.status)}`;
    elements.projectCheckedAt.textContent = `Integrity checked: ${formatDate(detail.checked_at)}`;
    elements.projectSummaryCards.innerHTML = buildSummaryCards(detail);
    renderManifestTree(detail);
    renderProjectHistory(detail.commits || []);

    elements.projectPreviewEmpty.classList.add("hidden");
    elements.projectPreview.classList.remove("hidden");
    elements.refreshProjectButton.disabled = false;
    elements.deleteProjectButton.disabled = false;
    showDeleteResult("");
    renderProjectsList(state.projects);
}

function chooseSelectedProject(projects, preferredProject) {
    if (!projects.length) {
        return null;
    }
    if (preferredProject && projects.some((item) => item.project === preferredProject)) {
        return preferredProject;
    }
    return projects[0].project;
}

async function selectProject(project, options = {}) {
    if (!project) {
        return;
    }

    const requestId = ++state.projectRequestId;
    state.selectedProject = project;
    renderProjectsList(state.projects);
    renderProjectLoading(project);
    if (options.syncHash !== false) {
        writeProjectHash(project);
    }

    try {
        const detail = await apiRequest(`/api/dashboard/projects/${encodeURIComponent(project)}`);
        if (requestId !== state.projectRequestId) {
            return;
        }
        renderProjectDetail(detail);
    } catch (error) {
        if (requestId !== state.projectRequestId) {
            return;
        }
        renderProjectError(project, error);
        throw error;
    }
}

async function refreshDashboard(options = {}) {
    try {
        const authenticated = await ensureAuthenticated();
        if (!authenticated) {
            return;
        }

        const [status, commits, logs, projectsPayload] = await Promise.all([
            apiRequest("/api/dashboard/status"),
            apiRequest("/api/dashboard/commits?limit=50"),
            apiRequest("/api/dashboard/logs?limit=200"),
            apiRequest("/api/dashboard/projects"),
        ]);

        const projects = projectsPayload.items || [];
        state.projects = projects;

        renderStatus(status);
        renderHistory(commits.items || []);
        renderLogs(logs);
        renderProjectsList(projects);

        const preferredProject = options.preferredProject ?? readProjectHash() ?? state.selectedProject;
        const nextProject = chooseSelectedProject(projects, preferredProject);

        if (!nextProject) {
            state.selectedProject = null;
            state.projectDetail = null;
            writeProjectHash(null);
            renderProjectEmpty("No projects stored on the server yet.");
            elements.passwordUpdatedMeta.textContent = "Protected by shared server password";
            return;
        }

        if (options.refreshSelectedProject || state.selectedProject !== nextProject || !state.projectDetail) {
            await selectProject(nextProject, { syncHash: true });
        } else {
            renderProjectsList(projects);
        }

        elements.passwordUpdatedMeta.textContent = "Protected by shared server password";
    } catch (error) {
        if (error.status === 401 || error.status === 503) {
            lockDashboard(error.message || "Authentication required.");
            return;
        }
        renderFailure(error);
    } finally {
        if (state.sessionToken) {
            scheduleRefresh();
        }
    }
}

async function handleDeleteProject() {
    const project = state.selectedProject;
    if (!project) {
        return;
    }

    const confirmed = window.confirm(
        `Delete project "${project}" from the server?\n\nThis will remove the latest manifest, commit history, pending sync state, and prune orphaned objects.`,
    );
    if (!confirmed) {
        return;
    }

    elements.deleteProjectButton.disabled = true;
    showDeleteResult("Deleting project from server...", "info");

    try {
        const result = await apiRequest(`/sync/reset/${encodeURIComponent(project)}`, { method: "POST" });
        showDeleteResult(
            `${result.message} Removed commits: ${result.removed_commits}, pending syncs: ${result.removed_pending_syncs}, objects pruned: ${result.removed_objects}.`,
            "success",
        );
        state.selectedProject = null;
        state.projectDetail = null;
        writeProjectHash(null);
        await refreshDashboard({ preferredProject: null, refreshSelectedProject: false });
    } catch (error) {
        elements.deleteProjectButton.disabled = false;
        if (error.status === 401 || error.status === 503) {
            lockDashboard(error.message || "Authentication required.");
            return;
        }
        showDeleteResult(error.message || String(error), "error");
    }
}

function handleHashChange() {
    const project = readProjectHash();
    if (!project || project === state.selectedProject) {
        return;
    }
    if (state.projects.some((item) => item.project === project)) {
        selectProject(project, { syncHash: false }).catch((error) => {
            renderFailure(error);
        });
    }
}

function initEvents() {
    elements.authForm.addEventListener("submit", submitAuth);
    elements.changePasswordForm.addEventListener("submit", handleChangePassword);
    elements.logoutButton.addEventListener("click", () => {
        lockDashboard("Dashboard locked. Enter the password again to continue.");
    });
    elements.refreshButton.addEventListener("click", () => {
        clearTimeout(state.refreshHandle);
        refreshDashboard({ refreshSelectedProject: true });
    });
    elements.refreshProjectButton.addEventListener("click", () => {
        if (!state.selectedProject) {
            refreshDashboard({ refreshSelectedProject: true });
            return;
        }
        selectProject(state.selectedProject).catch((error) => {
            renderFailure(error);
        });
    });
    elements.themeToggleButton.addEventListener("click", toggleTheme);
    elements.deleteProjectButton.addEventListener("click", () => {
        handleDeleteProject().catch((error) => {
            showDeleteResult(error.message || String(error), "error");
        });
    });
    window.addEventListener("hashchange", handleHashChange);
}

async function init() {
    applyTheme(state.theme);
    initEvents();
    if (!state.sessionToken) {
        redirectToAuthPage();
        return;
    }
    renderSessionState();
    renderProjectEmpty("Loading server projects.");
    try {
        await getAuthStatus();
    } catch (error) {
        renderFailure(error);
        renderAuthOverlay();
        return;
    }
    await refreshDashboard({ refreshSelectedProject: true });
}

init().catch(renderFailure);

