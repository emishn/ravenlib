const SESSION_STORAGE_KEY = "ravenlib-dashboard-session";

const state = {
    theme: localStorage.getItem("ravenlib-server-theme") || "light",
    sessionToken: localStorage.getItem(SESSION_STORAGE_KEY) || "",
    authStatus: null,
};

const elements = {
    authForm: document.getElementById("authForm"),
    authTitle: document.getElementById("authTitle"),
    authSubtitle: document.getElementById("authSubtitle"),
    authPassword: document.getElementById("authPassword"),
    authConfirmRow: document.getElementById("authConfirmRow"),
    authConfirmPassword: document.getElementById("authConfirmPassword"),
    authMessage: document.getElementById("authMessage"),
    authSubmitButton: document.getElementById("authSubmitButton"),
};

function getNextPath() {
    const params = new URLSearchParams(window.location.search);
    const next = params.get("next") || "/";
    if (!next.startsWith("/") || next.startsWith("//")) {
        return "/";
    }
    return next;
}

function applyTheme(theme) {
    state.theme = theme === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = state.theme;
    localStorage.setItem("ravenlib-server-theme", state.theme);
}

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
}

function setAuthMessage(message, mode = "info") {
    elements.authMessage.textContent = message || "";
    elements.authMessage.className = `auth-message ${message ? `danger-result-${mode}` : ""}`.trim();
}

function renderAuthPage() {
    const bootstrap = state.authStatus?.requires_bootstrap;
    elements.authTitle.textContent = bootstrap ? "Set Server Password" : "Server Login";
    elements.authSubtitle.textContent = bootstrap
        ? "Create the shared server password. This password will protect sync requests, the dashboard, and client access."
        : "Enter the shared server password to unlock the dashboard and protected sync API.";
    elements.authConfirmRow.classList.toggle("hidden", !bootstrap);
    elements.authConfirmPassword.required = bootstrap;
    elements.authSubmitButton.textContent = bootstrap ? "Save Password" : "Unlock Dashboard";
    elements.authPassword.focus();
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
        window.location.replace(getNextPath());
    } catch (error) {
        setAuthMessage(error.message || String(error), "error");
    } finally {
        elements.authSubmitButton.disabled = false;
    }
}

async function init() {
    applyTheme(state.theme);
    elements.authForm.addEventListener("submit", submitAuth);

    try {
        const authStatus = await getAuthStatus();
        if (state.sessionToken && !authStatus.requires_bootstrap) {
            await apiRequest("/api/dashboard/status");
            window.location.replace(getNextPath());
            return;
        }
        renderAuthPage();
    } catch (_error) {
        renderAuthPage();
    }
}

init().catch((error) => {
    setAuthMessage(error.message || String(error), "error");
});

