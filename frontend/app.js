// ── Config ────────────────────────────────────────────────────
const API_BASE = "http://localhost:8000";

const KEYCLOAK_CONFIG = {
  url: "http://localhost:8080",
  realm: "graphrag",
  clientId: "graphrag-api",
};

let keycloak = null;
let token = null;

const $ = (id) => document.getElementById(id);
const messagesEl = $("messages");
const formEl = $("ask-form");
const inputEl = $("question");
const sendBtn = $("send-btn");
const loginBtn = $("login-btn");
const logoutBtn = $("logout-btn");
const usernameEl = $("username");

const log = (...args) => console.log("[graphrag]", ...args);
const warn = (...args) => console.warn("[graphrag]", ...args);

// ── Init Keycloak ─────────────────────────────────────────────
async function initKeycloak() {
  log("initializing Keycloak…");

  if (typeof Keycloak === "undefined") {
    warn("Keycloak library not loaded.");
    usernameEl.textContent = "Keycloak library missing";
    return;
  }

  keycloak = new Keycloak(KEYCLOAK_CONFIG);

  try {
    const authenticated = await keycloak.init({
      onLoad: "check-sso",
      pkceMethod: "S256",
      checkLoginIframe: false,
    });

    log("Keycloak init complete. authenticated =", authenticated);

    if (authenticated) {
      token = keycloak.token;
      log("access token length =", token ? token.length : 0);
      renderUser();
      scheduleTokenRefresh();
    } else {
      renderAnonymous();
    }
  } catch (err) {
    warn("Keycloak init failed:", err);
    renderAnonymous();
    usernameEl.textContent = "auth error — see console";
  }
}

function scheduleTokenRefresh() {
  setInterval(async () => {
    try {
      await keycloak.updateToken(30);
      token = keycloak.token;
    } catch (err) {
      warn("token refresh failed:", err);
      token = null;
    }
  }, 20000);
}

function renderUser() {
  const name = keycloak.tokenParsed?.preferred_username || "user";
  usernameEl.textContent = name;
  loginBtn.hidden = true;
  loginBtn.disabled = false;
  logoutBtn.hidden = false;
  inputEl.disabled = false;
  sendBtn.disabled = false;
}

function renderAnonymous() {
  usernameEl.textContent = "not signed in";
  loginBtn.hidden = false;
  loginBtn.disabled = false;
  logoutBtn.hidden = true;
  inputEl.disabled = true;
  sendBtn.disabled = true;
}

function addMessage(text, kind = "bot", extra = null) {
  const div = document.createElement("div");
  div.className = `msg ${kind}`;
  div.textContent = text;

  if (extra?.documents?.length) {
    const docs = document.createElement("div");
    docs.className = "docs";
    extra.documents.forEach((d, i) => {
      const row = document.createElement("div");
      const authors = (d.authors || []).join(", ") || "—";
      const topics = (d.topics || []).join(", ") || "—";
      row.textContent = `${i + 1}. ${d.title}  [${topics}]  by ${authors}`;
      docs.appendChild(row);
    });
    div.appendChild(docs);
  }

  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

loginBtn.addEventListener("click", () => {
  log("login clicked");
  keycloak.login({ redirectUri: window.location.origin + "/" });
});

logoutBtn.addEventListener("click", () => {
  log("logout clicked");
  token = null;
  keycloak.logout({ redirectUri: window.location.origin + "/" });
});

formEl.addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = inputEl.value.trim();
  if (!question) return;
  if (!token) {
    addMessage("Please log in first.", "error");
    return;
  }

  addMessage(question, "user");
  inputEl.value = "";
  sendBtn.disabled = true;

  try {
    const resp = await fetch(`${API_BASE}/ask`, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ question }),
    });

    if (resp.status === 401) {
      addMessage("Session expired. Please log in again.", "error");
      token = null;
      renderAnonymous();
      return;
    }

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: "Unknown error" }));
      addMessage(`Error: ${err.detail || resp.statusText}`, "error");
      return;
    }

    const data = await resp.json();
    addMessage(data.answer || "(no answer)", "bot", {
      documents: data.documents || [],
    });
  } catch (err) {
    addMessage(`Network error: ${err.message}`, "error");
  } finally {
    sendBtn.disabled = false;
    inputEl.focus();
  }
});

// ── Start — wait for the module to load ───────────────────────
log("app.js loaded");

if (typeof Keycloak === "function") {
  initKeycloak();
} else {
  window.addEventListener("keycloak-ready", initKeycloak, { once: true });
}