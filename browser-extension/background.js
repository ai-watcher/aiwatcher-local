const STORAGE_KEY = "aiwatcher_enabled";
const PORT_START = 8765;
const PORT_END = 8784;
let cachedBaseUrl = null;

if (typeof chrome !== "undefined") {
  chrome.runtime.onInstalled.addListener(() => {
    chrome.storage.local.set({ [STORAGE_KEY]: true });
  });
}

async function fetchJson(url, options = {}, timeoutMs = 1500) {
  const response = await fetch(url, {
    ...options,
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function healthAt(baseUrl) {
  const health = await fetchJson(`${baseUrl}/api/health`);
  if (health.service !== "aiwatcher-local") throw new Error("Unexpected local service");
  return health;
}

async function discoverServer() {
  if (cachedBaseUrl) {
    try {
      const health = await healthAt(cachedBaseUrl);
      return { baseUrl: cachedBaseUrl, health };
    } catch {
      cachedBaseUrl = null;
    }
  }
  for (let port = PORT_START; port <= PORT_END; port += 1) {
    const baseUrl = `http://127.0.0.1:${port}`;
    try {
      const health = await healthAt(baseUrl);
      cachedBaseUrl = baseUrl;
      return { baseUrl, health };
    } catch {
      // Try the next AIWatcher fallback port.
    }
  }
  throw new Error(`AIWatcher Local was not found on ports ${PORT_START}-${PORT_END}`);
}

if (typeof chrome !== "undefined") {
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || !["health", "preflight"].includes(message.type)) return false;
    (async () => {
      try {
        const server = await discoverServer();
        if (message.type === "health") {
          sendResponse({ ok: true, baseUrl: server.baseUrl, health: server.health });
          return;
        }
        const result = await fetchJson(`${server.baseUrl}/api/preflight`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            prompt: String(message.prompt ?? ""),
            tool: String(message.tool ?? "claude-web"),
            cwd: null,
          }),
        }, 4000);
        sendResponse({ ok: true, result, baseUrl: server.baseUrl });
      } catch (error) {
        sendResponse({ ok: false, error: String(error?.message ?? error) });
      }
    })();
    return true;
  });
}

if (typeof module !== "undefined") {
  module.exports = {
    discoverServer,
    resetServerCache: () => { cachedBaseUrl = null; },
  };
}
