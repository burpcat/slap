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

async function handle(msg, sender) {
  const tabKey = `tab:${sender.tab && sender.tab.id}`;
  switch (msg.type) {
    case "check":
      return api(`/api/check?${new URLSearchParams({ company: msg.company || "", url: msg.url })}`);
    case "resumes":
      return api("/api/resumes");
    case "capture":
      return post("/api/leads", msg.job);
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

// Any other site: click the toolbar icon to open the panel there.
chrome.action.onClicked.addListener((tab) => {
  chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["lib.js", "content.js"] });
});
