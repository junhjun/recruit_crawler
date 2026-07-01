const button = document.getElementById("capture");
const statusEl = document.getElementById("status");

function setStatus(message) {
  statusEl.textContent = message;
}

async function sendCaptureCommand(tabId) {
  try {
    return await chrome.tabs.sendMessage(
      tabId,
      {
        type: "recruit-capture:capture-visible-postings",
        source: "popup",
        options: { download: true }
      },
      { frameId: 0 }
    );
  } catch (_error) {
    await chrome.scripting.executeScript({
      target: { tabId, frameIds: [0] },
      files: ["content.js"]
    });
    return chrome.tabs.sendMessage(
      tabId,
      {
        type: "recruit-capture:capture-visible-postings",
        source: "popup",
        options: { download: true }
      },
      { frameId: 0 }
    );
  }
}

button.addEventListener("click", async () => {
  button.disabled = true;
  setStatus("Capturing current tab...");
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id) {
      throw new Error("No active tab found.");
    }

    const result = await sendCaptureCommand(tab.id);
    if (!result || !result.ok) {
      throw new Error(result && result.error ? result.error : "Capture failed.");
    }
    const payload = result.payload || {};
    const sourceId = payload.source_id || "unknown";
    const count = result.validation && Number.isInteger(result.validation.posting_count)
      ? result.validation.posting_count
      : (Array.isArray(payload.postings) ? payload.postings.length : 0);
    setStatus(`Saved ${count} visible posting(s) from ${sourceId}.`);
  } catch (error) {
    setStatus(error && error.message ? error.message : String(error));
  } finally {
    button.disabled = false;
  }
});
