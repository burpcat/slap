// On-page panel: capture this posting into slap, show prior contact + the
// matched résumé, and record the application once you've submitted. Never
// clicks Submit and never records an application without your click.
(() => {
  if (window.__slap) return;
  window.__slap = true;
  const L = globalThis.slapLib;
  const send = (msg) => chrome.runtime.sendMessage(msg);
  const JD_MAX = 20000; // ponytail: hard cap (~5k tokens) on fallback page text; raise if JDs get cut

  /** The posting on this page, or null on an apply/confirmation step without one. */
  function extractJob() {
    const url = L.canonicalJobUrl(location.href);
    const ld = L.parseJobPosting([...document.querySelectorAll('script[type="application/ld+json"]')]
      .map((s) => s.textContent));
    if (ld) return { ...ld, url };
    if (L.isStepPage(location.href)) return null; // form/confirmation text is not a JD
    const main = document.querySelector("main, [role=main], #content") || document.body;
    const h1 = document.querySelector("h1");
    return { url, role: (h1 ? h1.innerText : document.title).trim(), company: "",
             jd: main.innerText.trim().slice(0, JD_MAX) };
  }

  // --- panel (shadow DOM: page CSS can't touch it, ours can't leak) ---------
  const host = document.createElement("div");
  host.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
    <style>
      .p{font:13px/1.4 system-ui,sans-serif;background:#fff;color:#1a1a1a;border:1px solid #d0d0d0;
         border-radius:10px;box-shadow:0 4px 18px rgba(0,0,0,.15);padding:10px 12px;width:290px}
      .h{display:flex;justify-content:space-between;font-weight:600;margin-bottom:4px}
      .job{color:#555;margin-bottom:6px}.lines div{margin:2px 0}.err{color:#b00020}
      .confirm{background:#fff6d6;border-radius:6px;padding:6px;margin:6px 0}
      button,select{font:inherit;margin-top:6px}button{cursor:pointer}
      .x{border:0;background:none;color:#888;margin:0}
      @media (prefers-color-scheme:dark){.p{background:#1f1f1f;color:#eee;border-color:#444}.job{color:#aaa}
        .confirm{background:#3a3110}}
    </style>
    <div class="p">
      <div class="h"><span>slap</span><button class="x" title="Hide">✕</button></div>
      <div class="job"></div><div class="lines"></div>
      <div class="confirm" hidden>Looks like you submitted. Record it so the held email can go out.</div>
      <div><button class="capture" hidden>Send to slap</button></div>
      <div class="applied" hidden>
        <select aria-label="Résumé you uploaded"></select> <button class="mark">Mark applied</button>
      </div>
      <div class="status"></div>
    </div>`;
  const $ = (sel) => root.querySelector(sel);
  $(".x").onclick = () => host.remove();
  document.documentElement.appendChild(host);

  const status = (text, isErr) => { $(".status").textContent = text || ""; $(".status").className = isErr ? "status err" : "status"; };

  let job = null;
  const safeRender = () => render().catch((e) => status(String(e), true));

  async function render() {
    const page = extractJob();
    const url = L.canonicalJobUrl(location.href);
    if (page) {
      job = page;
      send({ type: "rememberJob", job });
    } else { // apply/confirmation step: reuse the posting this tab came from
      const recalled = (await send({ type: "recallJob" })).data;
      job = recalled && recalled.url === url ? recalled : (job || { url, role: "", company: "", jd: "" });
    }
    $(".job").textContent = [job.company, job.role].filter(Boolean).join(" · ") || job.url;

    const [check, resumes] = await Promise.all([
      send({ type: "check", company: job.company, url: job.url }), send({ type: "resumes" })]);
    if (!check.ok) return status(check.error, true);
    $(".lines").replaceChildren(...L.badgeLines(check.data).map((t) => Object.assign(document.createElement("div"), { textContent: t })));

    const lead = check.data.lead;
    $(".capture").hidden = Boolean(lead && lead.jd) || !job.jd;
    const tags = resumes.ok ? resumes.data.resumes.map((r) => r.tag) : [];
    const preferred = lead && (lead.applied_resume || lead.resume);
    $(".applied select").replaceChildren(...tags.map((t) => new Option(t, t, false, t === preferred)));
    $(".applied").hidden = !tags.length || Boolean(lead && lead.applied_resume);
    $(".confirm").hidden = !(L.isConfirmation(location.href, document.body.innerText) && !(lead && lead.applied_resume));
  }

  $(".capture").onclick = async () => {
    const r = await send({ type: "capture", job });
    r.ok ? status(r.data.created ? "Captured. Run slap_leads in Claude Code to match + draft." : "Already captured.")
         : status(r.error, true);
    safeRender();
  };
  $(".mark").onclick = async () => {
    const resume = $(".applied select").value;
    const r = await send({ type: "applied", url: job.url, resume });
    r.ok ? status(`Recorded: applied with "${resume}".`) : status(r.error, true);
    safeRender();
  };

  // After a submit click, single-page sites (Ashby, Workday) swap in their
  // confirmation without navigating: re-check for a minute.
  document.addEventListener("submit", () => {
    let n = 0;
    const t = setInterval(() => { safeRender(); if (++n >= 30) clearInterval(t); }, 2000);
  }, true);

  // SPA navigation (Workday): re-render when the URL changes.
  let last = location.href;
  setInterval(() => { if (location.href !== last) { last = location.href; safeRender(); } }, 1000);

  safeRender();
})();
