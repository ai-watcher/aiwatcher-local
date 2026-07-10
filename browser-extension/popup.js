const STORAGE_KEY = "aiwatcher_enabled";

const dot = document.getElementById("statusDot");
const label = document.getElementById("statusLabel");
const toggle = document.getElementById("enabledToggle");

async function checkServer() {
  try {
    const response = await chrome.runtime.sendMessage({ type: "health" });
    if (!response?.ok) throw new Error(response?.error ?? "Unavailable");
    const port = new URL(response.baseUrl).port;
    dot.className = "status-dot online";
    label.textContent = `Connected locally on :${port}`;
  } catch {
    dot.className = "status-dot offline";
    label.textContent = "Offline - run aiwatcher ui";
  }
}

chrome.storage.local.get([STORAGE_KEY], (result) => {
  toggle.checked = result[STORAGE_KEY] !== false;
});

toggle.addEventListener("change", () => {
  chrome.storage.local.set({ [STORAGE_KEY]: toggle.checked });
});

checkServer();
