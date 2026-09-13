const AUTO_RESTART_DELAY_MS = 5000;
const UI_HEARTBEAT_INTERVAL_MS = 5000;
const CONFIG_SAVE_DELAY_MS = 300;

function createUiSessionId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

const state = {
    currentOperationId: null,
    pollHandle: null,
    restartHandle: null,
    configSaveHandle: null,
    uiHeartbeatHandle: null,
    lastOperationPath: null,
    restartAttempts: 0,
    autoRestartEnabled: localStorage.getItem("ravenlib-auto-restart") === "true",
    theme: document.documentElement.dataset.theme === "light" ? "light" : "dark",
    uiSessionId: createUiSessionId(),
    uiSessionClosed: false,
    isShuttingDown: false,
};

const elements = {
    serverUrl: document.getElementById("serverUrl"),
    projectName: document.getElementById("projectName"),
    projectDir: document.getElementById("projectDir"),
    commitMessage: document.getElementById("commitMessage"),
    saveConfigButton: document.getElementById("saveConfigButton"),
    syncButton: document.getElementById("syncButton"),
    pullButton: document.getElementById("pullButton"),
    endSessionButton: document.getElementById("endSessionButton"),
    autoRestartCheckbox: document.getElementById("autoRestartCheckbox"),
    autoRestartHint: document.getElementById("autoRestartHint"),
    checkConnectionButton: document.getElementById("checkConnectionButton"),
    clearLogButton: document.getElementById("clearLogButton"),
    connectionState: document.getElementById("connectionState"),
    operationType: document.getElementById("operationType"),
    operationStatus: document.getElementById("operationStatus"),
    operationMeta: document.getElementById("operationMeta"),
    progressBar: document.getElementById("progressBar"),
    resultCard: document.getElementById("resultCard"),
    resultContent: document.getElementById("resultContent"),
    activityLog: document.getElementById("activityLog"),
    themeToggleButton: document.getElementById("themeToggleButton"),
    themeToggleLabel: document.getElementById("themeToggleLabel"),
};

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

async function apiRequest(path, options = {}) {
    let response;
    try {
        response = await fetch(path, {
            headers: { "Content-Type": "application/json" },
            ...options,
        });
    } catch (error) {
        throw new Error(`Request failed: ${error.message || error}`);
    }

    const text = await response.text();
    let payload = {};
    if (text) {
        try { payload = JSON.parse(text); } catch { payload = { error: text }; }
    }
    if (!response.ok) {
        throw new Error(payload.error || payload.detail || `Request failed with ${response.status}`);
    }
    return payload;
}

function configPayload() {
    return {
        server_url: elements.serverUrl.value.trim(),
        project_name: elements.projectName.value.trim(),
        project_dir: elements.projectDir.value.trim(),
        commit_message: elements.commitMessage.value.trim(),
        theme: state.theme,
    };
}

function appendLog(message, level = "info") {
    const prefix = level === "error" ? "[ERROR] " : level === "warning" ? "[WARN] " : "";
    const current = elements.activityLog.textContent.trim();
    elements.activityLog.textContent = `${current && current !== "Operation logs will appear here." ? `${current}\n` : ""}${prefix}${message}`;
    elements.activityLog.scrollTop = elements.activityLog.scrollHeight;
}

function renderConnectionStatus(status, message = "") {
    const online = Boolean(status?.connected);
    elements.connectionState.textContent = message || (online ? "Connected" : "Offline");
    elements.connectionState.className = `status-badge ${online ? "status-success" : "status-danger"}`;
}

function renderOperation(operation) {
    if (!operation) return;
    elements.operationType.textContent = operation.operation_type || "Operation";
    elements.operationStatus.textContent = operation.status_text || operation.state || "Running";
    elements.operationMeta.textContent = operation.project_name || "";
    const current = Number(operation.progress_current || 0);
    const total = Number(operation.progress_total || 0);
    elements.progressBar.style.width = total > 0 ? `${Math.min(100, Math.round((current / total) * 100))}%` : "12%";

    if (Array.isArray(operation.logs)) {
        elements.activityLog.textContent = operation.logs.map((item) => item.message || item).join("\n") || "Operation logs will appear here.";
        elements.activityLog.scrollTop = elements.activityLog.scrollHeight;
    }

    if (operation.result || operation.error) {
        elements.resultCard.classList.remove("result-empty");
        const result = operation.result || {};
        const rows = [];
        if (operation.error) rows.push(`<div class="danger-result">${escapeHtml(operation.error)}</div>`);
        for (const [key, value] of Object.entries(result)) {
            if (value === null || value === undefined || typeof value === "object") continue;
            rows.push(`<div><strong>${escapeHtml(key)}:</strong> ${escapeHtml(value)}</div>`);
        }
        elements.resultContent.innerHTML = rows.join("") || escapeHtml(operation.status_text || "");
    }
}

function renderError(error) {
    elements.resultCard.classList.remove("result-empty");
    elements.resultContent.innerHTML = `<div class="danger-result">${escapeHtml(error.message || error)}</div>`;
    appendLog(error.message || error, "error");
}

function setButtonsDisabled(disabled) {
    elements.syncButton.disabled = disabled;
    elements.pullButton.disabled = disabled;
    elements.saveConfigButton.disabled = disabled;
}

function scheduleSilentConfigSave() {
    clearTimeout(state.configSaveHandle);
    state.configSaveHandle = setTimeout(() => saveConfig(false).catch(() => {}), CONFIG_SAVE_DELAY_MS);
}

async function saveConfig(showToast = true) {
    const payload = await apiRequest("/api/config", { method: "POST", body: JSON.stringify(configPayload()) });
    if (showToast) {
        elements.resultCard.classList.remove("result-empty");
        elements.resultContent.innerHTML = "<div><strong>Config saved.</strong></div>";
    }
    return payload;
}

async function loadConfig() {
    const payload = await apiRequest("/api/config");
    elements.serverUrl.value = payload.server_url || "";
    elements.projectName.value = payload.project_name || "";
    elements.projectDir.value = payload.project_dir || "";
    elements.commitMessage.value = payload.commit_message || "";
    if (payload.theme) applyTheme(payload.theme, { persistConfig: false });
}

async function checkConnection() {
    try {
        const query = new URLSearchParams({ server_url: elements.serverUrl.value.trim() });
        const status = await apiRequest(`/api/connection-status?${query.toString()}`);
        renderConnectionStatus(status);
    } catch (error) {
        renderConnectionStatus(null, "Offline");
    }
}

function operationName(path) { return path === "/api/pull" ? "pull" : "sync"; }

function updateAutoRestartHint(message = "") {
    elements.autoRestartHint.textContent = message || (state.autoRestartEnabled
        ? "Auto restart is enabled for sync and pull errors."
        : "Enable Auto restart to retry sync and pull automatically after errors.");
}

function setAutoRestartEnabled(enabled) {
    state.autoRestartEnabled = Boolean(enabled);
    localStorage.setItem("ravenlib-auto-restart", String(state.autoRestartEnabled));
    elements.autoRestartCheckbox.checked = state.autoRestartEnabled;
    if (!state.autoRestartEnabled) {
        clearTimeout(state.restartHandle);
        state.restartHandle = null;
        state.restartAttempts = 0;
    }
    updateAutoRestartHint();
}

function stopPolling() {
    clearTimeout(state.pollHandle);
    state.pollHandle = null;
}

function scheduleAutoRestart(path, reason = "") {
    if (!state.autoRestartEnabled || !path || state.restartHandle) return false;
    state.restartAttempts += 1;
    const label = operationName(path);
    const text = `Retrying ${label} in ${AUTO_RESTART_DELAY_MS / 1000}s (attempt ${state.restartAttempts}).`;
    updateAutoRestartHint(reason ? `Auto restart: ${text} ${reason}` : `Auto restart: ${text}`);
    state.restartHandle = setTimeout(() => {
        state.restartHandle = null;
        startOperation(path, { isAutoRestart: true }).catch(renderError);
    }, AUTO_RESTART_DELAY_MS);
    return true;
}

async function startOperation(path, { isAutoRestart = false } = {}) {
    if (state.currentOperationId && !isAutoRestart) return;
    stopPolling();
    setButtonsDisabled(true);
    state.lastOperationPath = path;
    try {
        await saveConfig(false);
        const operation = await apiRequest(path, { method: "POST", body: JSON.stringify(configPayload()) });
        state.currentOperationId = operation.operation_id;
        elements.resultCard.classList.add("result-empty");
        elements.resultContent.textContent = "Operation started.";
        pollOperation();
    } catch (error) {
        setButtonsDisabled(false);
        renderError(error);
        scheduleAutoRestart(path, error.message || String(error));
    }
}

async function pollOperation() {
    if (!state.currentOperationId) return;
    try {
        const operation = await apiRequest(`/api/operations/${state.currentOperationId}`);
        renderOperation(operation);
        if (operation.state === "completed") {
            stopPolling(); state.currentOperationId = null; setButtonsDisabled(false);
            state.restartAttempts = 0; updateAutoRestartHint(); return;
        }
        if (operation.state === "failed") {
            stopPolling(); state.currentOperationId = null; setButtonsDisabled(false);
            scheduleAutoRestart(state.lastOperationPath, operation.error || "Operation failed."); return;
        }
    } catch (error) {
        stopPolling(); state.currentOperationId = null; setButtonsDisabled(false);
        renderError(error); scheduleAutoRestart(state.lastOperationPath, error.message || String(error)); return;
    }
    state.pollHandle = setTimeout(pollOperation, 800);
}

async function registerUiSession() {
    await apiRequest("/api/ui-session/open", { method: "POST", body: JSON.stringify({ session_id: state.uiSessionId }) });
    state.uiSessionClosed = false;
    state.uiHeartbeatHandle = setInterval(() => {
        if (!state.uiSessionClosed) apiRequest("/api/ui-session/ping", { method: "POST", body: JSON.stringify({ session_id: state.uiSessionId }) }).catch(() => {});
    }, UI_HEARTBEAT_INTERVAL_MS);
}

async function closeUiSession(reason = "pagehide") {
    if (state.uiSessionClosed) return;
    state.uiSessionClosed = true;
    clearInterval(state.uiHeartbeatHandle);
    try { await apiRequest("/api/ui-session/close", { method: "POST", body: JSON.stringify({ session_id: state.uiSessionId, reason }) }); } catch {}
}

async function endSession() {
    state.isShuttingDown = true;
    await closeUiSession("manual-end-session");
    await apiRequest("/api/shutdown", { method: "POST", body: JSON.stringify({ reason: "manual" }) });
}

function applyTheme(theme, { persistConfig = true } = {}) {
    state.theme = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = state.theme;
    elements.themeToggleLabel.textContent = state.theme === "dark" ? "Light theme" : "Dark theme";
    if (persistConfig) scheduleSilentConfigSave();
}

function initEvents() {
    elements.saveConfigButton.addEventListener("click", () => saveConfig().catch(renderError));
    elements.syncButton.addEventListener("click", () => startOperation("/api/sync"));
    elements.pullButton.addEventListener("click", () => startOperation("/api/pull"));
    elements.endSessionButton.addEventListener("click", () => endSession().catch(renderError));
    elements.checkConnectionButton.addEventListener("click", checkConnection);
    elements.clearLogButton.addEventListener("click", () => { elements.activityLog.textContent = "Operation logs will appear here."; });
    elements.themeToggleButton.addEventListener("click", () => applyTheme(state.theme === "dark" ? "light" : "dark"));
    elements.autoRestartCheckbox.addEventListener("change", () => setAutoRestartEnabled(elements.autoRestartCheckbox.checked));
    [elements.serverUrl, elements.projectName, elements.projectDir, elements.commitMessage].forEach((element) => {
        element.addEventListener("change", scheduleSilentConfigSave);
        element.addEventListener("blur", scheduleSilentConfigSave);
    });
    elements.serverUrl.addEventListener("change", checkConnection);
    window.addEventListener("pagehide", () => closeUiSession("pagehide"));
    window.addEventListener("beforeunload", () => closeUiSession("beforeunload"));
}

async function init() {
    applyTheme(state.theme, { persistConfig: false });
    setAutoRestartEnabled(state.autoRestartEnabled);
    initEvents();
    await registerUiSession();
    await loadConfig();
    await checkConnection();
}

init().catch(renderError);
