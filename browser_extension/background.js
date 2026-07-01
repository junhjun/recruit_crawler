function timestampSlug() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

function cleanSegment(value, fallback) {
  return String(value || fallback)
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60) || fallback;
}

function captureFilename(payload) {
  const sourceId = cleanSegment(payload && payload.source_id, "unknown");
  const date = new Date().toISOString().slice(0, 10);
  return `recruit-captures/${date}/${sourceId}/recruit-capture-${sourceId}-${timestampSlug()}.json`;
}

async function downloadPayload(payload) {
  const json = JSON.stringify(payload || {}, null, 2);
  const url = URL.createObjectURL(new Blob([json], { type: "application/json" }));
  const filename = captureFilename(payload);
  try {
    const downloadId = await chrome.downloads.download({
      url,
      filename,
      saveAs: false
    });
    return { downloadId, filename };
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "recruit-capture:download") return false;

  downloadPayload(message.payload)
    .then((download) => sendResponse({ ok: true, ...download }))
    .catch((error) => sendResponse({ ok: false, error: error && error.message ? error.message : String(error) }));
  return true;
});
