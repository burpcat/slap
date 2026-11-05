// Service worker: the ONLY code that talks to slap's local API. Content
// scripts run in the job page's origin and message here instead, so slap sees
// Origin chrome-extension://<this id> (pinned by config.yaml extension_id).
const API = "http://127.0.0.1:5050";
const DOWN = "slap isn't reachable at 127.0.0.1:5050. Start it with `python slap.py dashboard` " +
  "(or load the com.slap.dashboard LaunchAgent).";

async function api(path, init = {}) {
  let resp;
  try {
    resp = await fetch(API + path, { ...init, headers: { "Content-Type": "application/json" } });
  } catch {
    return { ok: false, error: DOWN };
  }
  const body = await resp.json().catch(() => ({}));
  if (resp.status === 403) {
    return { ok: false, error: `${body.error || "refused"}. Set extension_id: ${chrome.runtime.id} in config.yaml and restart slap.` };
  }
  return resp.ok ? { ok: true, data: body } : { ok: false, error: body.error || `HTTP ${resp.status}` };
}

const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });

// Résumé PDF bytes as base64 (extension messages carry JSON only).
async function resumeFile(tag) {
  let resp;
  try {
    resp = await fetch(`${API}/api/resumes/${encodeURIComponent(tag)}`);
  } catch {
    return { ok: false, error: DOWN };
  }
  if (!resp.ok) return { ok: false, error: (await resp.json().catch(() => ({}))).error || `HTTP ${resp.status}` };
  const bytes = new Uint8Array(await resp.arrayBuffer());
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return { ok: true, data: { base64: btoa(bin), filename: `${tag}.pdf` } };
}

async function handle(msg, sender) {
  const tabKey = `tab:${sender.tab && sender.tab.id}`;
  switch (msg.type) {
    case "check":
      return api(`/api/check?${new URLSearchParams({ company: msg.company || "", url: msg.url })}`);
    case "resumes":
      return api("/api/resumes");
    case "capture":
      return post("/api/leads", msg.job);
    case "profile":
      return api("/api/profile");
    case "questions":
      return post("/api/leads/questions", { url: msg.url, questions: msg.questions });
    case "resumeFile":
      return resumeFile(msg.tag);
    case "applied":
      return post("/api/leads/applied", { url: msg.url, resume: msg.resume });
    case "rememberJob": // the posting this tab is applying to, for its later apply/confirmation pages
      await chrome.storage.session.set({ [tabKey]: msg.job });
      return { ok: true };
    case "recallJob":
      return { ok: true, data: (await chrome.storage.session.get(tabKey))[tabKey] || null };
    default:
      return { ok: false, error: `unknown message ${msg.type}` };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  handle(msg, sender).then(reply);
  return true; // async reply
});

// Toolbar icon: re-show the panel if it's on this page (closed or not), or
// inject it on any other site.
chrome.action.onClicked.addListener(async (tab) => {
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "show" });
  } catch {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["lib.js", "content.js"] });
  }
});
