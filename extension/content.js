// On-page panel. On a posting: capture it with its campaign, résumé and
// contact. On an application form: prepare answers from your config.yaml
// profile for review, fill the ticked ones, attach the résumé, and send the
// leftovers to Claude. Clicking Applied is what stages the reach-out email.
// Never clicks Submit and never fills or records anything without your click.
(() => {
  if (window.__slap) return;
  window.__slap = true;
  const L = globalThis.slapLib;
  const send = (msg) => chrome.runtime.sendMessage(msg);
  const JD_MAX = 20000; // ponytail: hard cap (~5k tokens) on fallback page text; raise if JDs get cut
  const clean = (t) => (t || "").replace(/\s+/g, " ").trim();

  /** The posting on this page, or null on an apply/confirmation step without one. */
  function extractJob() {
    const url = L.canonicalJobUrl(location.href);
    const ld = L.parseJobPosting([...document.querySelectorAll('script[type="application/ld+json"]')]
      .map((s) => s.textContent));
    if (ld) return { ...ld, url };
    if (L.isStepPage(location.href)) return null; // form/confirmation text is not a JD
    const main = document.querySelector("main, [role=main], #content") || document.body;
    const h1 = document.querySelector("h1");
    return { url, role: clean(h1 ? h1.innerText : document.title), company: "",
             jd: main.innerText.trim().slice(0, JD_MAX) };
  }

  // --- form scanning + filling (decisions live in lib.planFill) -------------
  const TEXTY = new Set(["text", "email", "tel", "url", "number", "search", ""]);
  const visible = (el) => Boolean(el.offsetParent || el.getClientRects().length);

  // textContent, not innerText: no forced layout, and it reads labels whose
  // text is visually hidden (screen-reader-only spans).
  // The field's own wrapper on common job sites (Ashby/Greenhouse/Lever class
  // names, Workday automation ids), where an unlinked <label> usually lives.
  const FIELD_BOX = '[data-automation-id^="formField"], fieldset, [class*="field" i], [class*="question" i], [class*="form-group" i]';

  function containerLabel(el) {
    const box = el.parentElement && el.parentElement.closest(FIELD_BOX);
    if (!box) return "";
    const head = [...box.querySelectorAll('label, legend, [class*="label" i]')]
      .find((h) => !h.contains(el) && !h.querySelector("input, select, textarea") && clean(h.textContent));
    return head ? clean(head.textContent) : "";
  }

  function labelOf(el) {
    const ids = el.getAttribute("aria-labelledby");
    return clean((el.labels && el.labels[0] && el.labels[0].textContent) || el.getAttribute("aria-label")
      || (ids && ids.split(/\s+/).map((id) => (document.getElementById(id) || {}).textContent || "").join(" "))
      || (el.closest("label") || {}).textContent || containerLabel(el) || el.placeholder || el.name);
  }

  function groupLabel(radio) {
    const box = radio.closest("fieldset, [role=radiogroup], .application-question, .field");
    const head = box && box.querySelector("legend, [class*=label]:not(label), label:not(:has(input))");
    return clean(head ? head.textContent : radio.name);
  }

  function scanForm() {
    const questions = [];
    const radioNames = new Set();
    for (const el of document.querySelectorAll("input, textarea, select")) {
      if (el.disabled || el.readOnly || !visible(el)) continue;
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (type === "radio") {
        if (!el.name || radioNames.has(el.name)) continue;
        radioNames.add(el.name);
        const els = [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)];
        questions.push({ label: groupLabel(el), kind: "radio", options: els.map(labelOf), els });
      } else if (el.tagName === "SELECT") {
        questions.push({ label: labelOf(el), kind: "select", els: [el],
                         options: [...el.options].filter((o) => o.value).map((o) => clean(o.text)) });
      } else if (el.tagName === "TEXTAREA" || TEXTY.has(type)) {
        questions.push({ label: labelOf(el), kind: el.tagName === "TEXTAREA" ? "textarea" : "text", els: [el] });
      }
    }
    return questions.filter((q) => q.label);
  }

  function setValue(el, value) { // native setter + events, so React/Vue-controlled inputs see it
    const proto = { TEXTAREA: HTMLTextAreaElement, SELECT: HTMLSelectElement }[el.tagName] || HTMLInputElement;
    Object.getOwnPropertyDescriptor(proto.prototype, "value").set.call(el, value);
    for (const t of ["input", "change", "blur"]) el.dispatchEvent(new Event(t, { bubbles: true }));
  }

  function fillOne(q, value) {
    if (q.kind === "radio") {
      const i = q.options.indexOf(value);
      if (i >= 0) q.els[i].click();
      return i >= 0;
    }
    if (q.kind === "select") {
      const opt = [...q.els[0].options].find((o) => clean(o.text) === value);
      if (opt) setValue(q.els[0], opt.value);
      return Boolean(opt);
    }
    setValue(q.els[0], value);
    return true;
  }

  function resumeInput() {
    const files = [...document.querySelectorAll("input[type=file]")];
    const text = (f) => `${labelOf(f)} ${f.name} ${f.id}`;
    return files.find((f) => /resume|cv\b/i.test(text(f)) && !/cover/i.test(text(f)))
      || (files.length === 1 ? files[0] : null);
  }

  // --- panel (shadow DOM: page CSS can't touch it, ours can't leak) ---------
  const host = document.createElement("div");
  host.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647";
  // Closed: the job page's scripts (and session-replay tools) can't reach in.
  const root = host.attachShadow({ mode: "closed" });
  // Styling follows the shadcn/ui design system (zinc tokens, 8px radius,
  // button/input/badge/tab styles), hand-written because the panel is plain JS
  // in a shadow root with no build step.
  root.innerHTML = `
    <style>
      :host{all:initial}
      .p{--bg:#ffffff;--fg:#09090b;--muted:#f4f4f5;--muted-fg:#71717a;--border:#e4e4e7;--primary:#18181b;
         --primary-fg:#fafafa;--ring:#a1a1aa;--accent:#2563eb;--accent-soft:#eff6ff;--ok:#15803d;--ok-soft:#f0fdf4;
         --warn:#b45309;--warn-soft:#fffbeb;--danger:#dc2626;--radius:8px;
         box-sizing:border-box;width:360px;max-height:min(640px,calc(100vh - 32px));display:flex;flex-direction:column;
         background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:12px;
         box-shadow:0 10px 30px -8px rgba(0,0,0,.25),0 2px 6px rgba(0,0,0,.06);
         font:13px/1.45 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;overflow:hidden}
      @media (prefers-color-scheme:dark){.p{--bg:#09090b;--fg:#fafafa;--muted:#18181b;--muted-fg:#a1a1aa;--border:#27272a;
         --primary:#fafafa;--primary-fg:#18181b;--ring:#52525b;--accent:#60a5fa;--accent-soft:#172554;--ok:#4ade80;
         --ok-soft:#052e16;--warn:#fbbf24;--warn-soft:#2a1d05;--danger:#f87171}}
      *{box-sizing:border-box}
      [hidden]{display:none!important}
      .hd{display:flex;align-items:center;justify-content:space-between;padding:12px 12px 0 14px}
      .brand{display:flex;align-items:center;gap:8px;font-weight:600;letter-spacing:-.01em}
      .logo{width:20px;height:20px;border-radius:6px;background:var(--primary);color:var(--primary-fg);display:grid;place-items:center;font-size:12px;font-weight:700}
      .title{padding:6px 14px 0}
      .job{font-size:14px;font-weight:600;line-height:1.3;overflow-wrap:anywhere}
      .lines{list-style:none;margin:6px 0 0;padding:0 14px;display:flex;flex-direction:column;gap:2px;color:var(--muted-fg);font-size:12px}
      .lines li.warn{color:var(--warn)}
      .badge{display:inline-flex;align-items:center;height:20px;padding:0 8px;border-radius:999px;font-size:11px;font-weight:500;border:1px solid var(--border);color:var(--muted-fg);background:var(--muted)}
      .badge.applied{color:var(--accent);background:var(--accent-soft);border-color:transparent}
      .badge.staged{color:var(--ok);background:var(--ok-soft);border-color:transparent}
      .tabs{display:flex;gap:2px;margin:10px 14px 0;padding:3px;background:var(--muted);border-radius:var(--radius)}
      .tab{flex:1;height:28px;border:0;border-radius:6px;background:transparent;color:var(--muted-fg);font:inherit;font-weight:500;cursor:pointer}
      .tab[aria-selected="true"]{background:var(--bg);color:var(--fg);box-shadow:0 1px 2px rgba(0,0,0,.08)}
      .tab .count{margin-left:4px;color:var(--muted-fg);font-size:11px}
      .scroll{overflow:auto;padding:12px 14px;display:flex;flex-direction:column;gap:12px;min-height:0}
      .tabpanel{display:flex;flex-direction:column;gap:12px}
      .card{border:1px solid var(--border);border-radius:var(--radius);padding:12px;display:flex;flex-direction:column;gap:10px}
      .fields{display:grid;grid-template-columns:1fr 1fr;gap:10px}
      .field{display:flex;flex-direction:column;gap:4px;min-width:0}
      .field.wide{grid-column:1/-1}
      label.l{font-size:12px;font-weight:500}
      input,select{font:inherit;color:var(--fg)}
      .in{height:32px;width:100%;padding:0 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);min-width:0}
      select.in{padding-right:6px}
      .in:focus-visible{outline:2px solid var(--ring);outline-offset:1px}
      .in:disabled{opacity:.6}
      .actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
      .btn{height:32px;padding:0 12px;border-radius:6px;font:inherit;font-weight:500;cursor:pointer;border:1px solid var(--border);
           background:var(--bg);color:var(--fg);display:inline-flex;align-items:center;gap:6px}
      .btn:hover{background:var(--muted)}
      .btn.primary{background:var(--primary);color:var(--primary-fg);border-color:var(--primary)}
      .btn.primary:hover{opacity:.9}
      .btn.ghost{border-color:transparent;background:transparent;color:var(--muted-fg)}
      .btn.ghost:hover{background:var(--muted);color:var(--fg)}
      .btn.danger{color:var(--danger);border-color:var(--danger)}
      .btn:disabled{opacity:.5;cursor:not-allowed}
      .btn:focus-visible,.tab:focus-visible,.icon:focus-visible,summary:focus-visible{outline:2px solid var(--ring);outline-offset:2px}
      .btn.block{width:100%;justify-content:center}
      .btn.grow{flex:1;justify-content:center}
      .toolbar .recheck{margin-left:auto}
      .icon{width:28px;height:28px;border:0;border-radius:6px;background:transparent;color:var(--muted-fg);display:grid;place-items:center;cursor:pointer}
      .icon:hover{background:var(--muted);color:var(--fg)}
      .muted{color:var(--muted-fg);font-size:12px}
      .email-status{font-weight:500}
      details.preview summary{cursor:pointer;color:var(--accent);font-weight:500;list-style:none}
      details.preview summary::-webkit-details-marker{display:none}
      details.preview[open] summary{margin-bottom:8px}
      .subj{font-size:12px;color:var(--muted-fg);margin-bottom:6px}
      pre{white-space:pre-wrap;margin:0;padding:10px;border-radius:6px;background:var(--muted);font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;max-height:220px;overflow:auto}
      .toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
      .switch{display:flex;gap:8px;align-items:flex-start;font-size:12px;color:var(--muted-fg);cursor:pointer}
      .switch input{margin:2px 0 0;accent-color:var(--primary)}
      .rows{display:flex;flex-direction:column;border:1px solid var(--border);border-radius:var(--radius)}
      .rows:empty{display:none}
      .row{padding:10px 12px;display:flex;flex-direction:column;gap:6px}
      .row+.row{border-top:1px solid var(--border)}
      .row label{display:flex;gap:8px;align-items:flex-start;cursor:pointer}
      .row label input{margin:2px 0 0;accent-color:var(--primary)}
      .row b{font-weight:500;overflow-wrap:anywhere}
      .row .v{height:30px;width:100%;padding:0 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg)}
      .row .v:disabled{background:var(--muted)}
      .src{font-size:11px;color:var(--muted-fg)}
      .src[data-src="claude"]{color:var(--accent)}
      .src[data-src="sensitive"],.src[data-src="eeo"]{color:var(--warn)}
      .ft{border-top:1px solid var(--border);padding:12px 14px;display:flex;flex-direction:column;gap:8px;background:var(--bg)}
      .confirm{background:var(--warn-soft);color:var(--warn);border-radius:6px;padding:8px 10px;font-size:12px}
      .status{font-size:12px;color:var(--muted-fg);min-height:0}
      .status:empty{display:none}
      .status.err{color:var(--danger)}
      @media (prefers-reduced-motion:no-preference){.btn,.tab,.icon{transition:background .15s,color .15s,opacity .15s}}
    </style>
    <div class="p" role="dialog" aria-label="slap">
      <div class="hd">
        <div class="brand"><span class="logo" aria-hidden="true">s</span><span>slap</span><span class="badge stage" hidden></span></div>
        <button class="x icon" aria-label="Close panel" title="Close (the toolbar icon brings it back)"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
      </div>
      <div class="title"><div class="job"></div></div>
      <ul class="lines"></ul>
      <div class="tabs" role="tablist" aria-label="Panel sections">
        <button class="tab" role="tab" data-tab="posting" aria-selected="true">Posting</button>
        <button class="tab" role="tab" data-tab="form" aria-selected="false" hidden>Application<span class="count"></span></button>
      </div>

      <div class="scroll">
        <section class="tabpanel" data-tab="posting" role="tabpanel">
          <div class="card setup">
            <div class="fields">
              <div class="field"><label class="l" for="campaign">Campaign</label><select class="in" id="campaign"></select></div>
              <div class="field"><label class="l" for="resume">Résumé</label><select class="in" id="resume"></select></div>
              <div class="field wide"><label class="l" for="cemail">Contact email</label><input class="in" id="cemail" type="email" placeholder="who gets the reach-out"></div>
              <div class="field wide"><label class="l" for="cname">Contact name</label><input class="in" id="cname" placeholder="first name"></div>
            </div>
            <div class="actions"><button class="btn primary capture">Capture posting</button><button class="btn save" hidden>Save details</button></div>
            <div class="muted capture-hint" hidden>Open the job posting page to capture it.</div>
          </div>
          <div class="card mail" hidden>
            <div class="email-status"></div>
            <details class="preview" hidden><summary>Preview email</summary><div class="subj"></div><pre class="body"></pre></details>
          </div>
        </section>

        <section class="tabpanel" data-tab="form" role="tabpanel" hidden>
          <div class="form" hidden>
            <div class="tabpanel">
              <div class="toolbar">
                <button class="btn attach" hidden>Attach résumé</button>
                <button class="btn ghost recheck">Re-check answers</button>
              </div>
              <label class="switch"><input type="checkbox" class="auto"><span>Send unanswered questions to Claude automatically after filling</span></label>
              <div class="rows"></div>
            </div>
          </div>
        </section>
      </div>

      <div class="ft">
        <div class="actions formbar" hidden><button class="btn go grow">Fill checked</button><button class="btn ask" hidden></button></div>
        <div class="confirm" hidden></div>
        <div class="actions"><button class="btn primary block applied">Applied</button><button class="btn danger anyway" hidden>Stage anyway</button></div>
        <div class="status" role="status" aria-live="polite"></div>
      </div>
    </div>`;
  const $ = (sel) => root.querySelector(sel);
  // Only a real click from you counts. A page script calling .click() makes an
  // untrusted event, and filling a form or recording an application (which
  // stages an email) must never happen on the page's say-so. The test hook is
  // safe in Chrome: content scripts run in an isolated world, so the page
  // can't set globals the extension reads.
  const trusted = (e) => e.isTrusted || globalThis.__slapTrustSyntheticClicksForTests === true;
  const onClick = (sel, fn) => {
    $(sel).onclick = (e) => {
      if (!trusted(e)) return;
      Promise.resolve(fn(e)).catch((err) => status(String(err.message || err), true));
    };
  };
  const status = (text, isErr) => { $(".status").textContent = text || ""; $(".status").className = isErr ? "status err" : "status"; };
  const el = (tag, props, ...kids) => { const e = Object.assign(document.createElement(tag), props); e.append(...kids); return e; };
  document.documentElement.appendChild(host);

  onClick(".x", () => { host.style.display = "none"; });
  // The toolbar icon brings a closed panel back (background.js sends "show").
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "show") { host.style.display = ""; safeRender(); }
  });

  let job = null;          // this page's posting (or the one this tab came from)
  let lead = null;         // slap's record of it
  let campaigns = [];      // [{name, resumes}]
  let detailsDirty = false; // your unsaved edits survive re-renders
  let questions = [];
  let plan = [];
  let preparedSig = "";
  let autoAsk = false;
  let attachedTag = null;  // the résumé you put into this form's upload field
  let activeTab = "posting";
  let tabPicked = false;    // you chose a tab; stop auto-switching
  function showTab(name) {
    activeTab = name;
    $(".formbar").hidden = name !== "form" || $(".form").hidden;
    root.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
    root.querySelectorAll("section.tabpanel").forEach((p) => { p.hidden = p.dataset.tab !== name; });
  }
  root.querySelectorAll(".tab").forEach((t) => {
    t.onclick = () => { tabPicked = true; showTab(t.dataset.tab); };
  });
  const safeRender = () => render().catch((e) => status(String(e.message || e), true));

  chrome.storage.local.get("autoAskClaude").then((v) => { autoAsk = Boolean(v.autoAskClaude); $(".auto").checked = autoAsk; },
                                                  () => {});
  $(".auto").onchange = (e) => {
    if (!trusted(e)) return;
    autoAsk = e.target.checked;
    chrome.storage.local.set({ autoAskClaude: autoAsk });
  };
  for (const id of ["#campaign", "#resume", "#cemail", "#cname"]) {
    $(id).addEventListener("input", (e) => { if (trusted(e)) detailsDirty = true; });
    $(id).addEventListener("change", (e) => { if (trusted(e)) detailsDirty = true; });
  }
  $("#campaign").addEventListener("change", () => fillResumeOptions($("#resume").value));

  function fillResumeOptions(preferred) {
    const camp = campaigns.find((c) => c.name === $("#campaign").value);
    const tags = camp ? camp.resumes : [];
    $("#resume").replaceChildren(...tags.map((t) => new Option(t, t, false, t === preferred)));
  }

  function details() {
    return { campaign: $("#campaign").value, resume: $("#resume").value,
             contact_email: $("#cemail").value.trim(), contact_name: $("#cname").value.trim() };
  }

  async function render() {
    const page = extractJob();
    const url = L.canonicalJobUrl(location.href);
    if (page) {
      job = page;
      send({ type: "rememberJob", job });
    } else { // apply/confirmation step: reuse the posting this tab came from
      const recalled = (await send({ type: "recallJob" })).data;
      job = recalled && recalled.url === url ? recalled
        : job && job.url === url ? job : { url, role: "", company: "", jd: "" };
    }
    $(".job").textContent = [job.company, job.role].filter(Boolean).join(" · ") || job.url;

    const [check, opts] = await Promise.all([
      send({ type: "check", company: job.company, url: job.url }), send({ type: "campaignOptions" })]);
    if (!check.ok) return status(check.error, true);
    if (!opts.ok) return status(opts.error, true);
    lead = check.data.lead;
    campaigns = opts.data.campaigns;
    const staged = Boolean(lead && lead.staged_to && lead.staged_to.length);

    const lines = L.badgeLines(check.data);
    if (!page && !lead) lines.unshift(`Not tied to a captured posting: anything you record here goes to ${job.url}`);
    $(".lines").replaceChildren(...lines.filter((t) => t !== "Captured").map((t) => el("li", { textContent: t, // the badge says it
      className: /^(Already contacted|Not tied)/.test(t) ? "warn" : "" })));
    const stage = staged ? ["Staged", "staged"] : lead && lead.applied_resume ? ["Applied", "applied"]
      : lead && lead.jd ? ["Captured", ""] : null;
    $(".stage").hidden = !stage;
    if (stage) { $(".stage").textContent = stage[0]; $(".stage").className = `badge stage ${stage[1]}`; }

    // setup: campaign / résumé / contact (left alone while you're editing)
    if (!detailsDirty) {
      $("#campaign").replaceChildren(new Option("Choose…", ""),
        ...campaigns.map((c) => new Option(c.name, c.name, false, lead && c.name === lead.campaign)));
      fillResumeOptions(lead && (lead.applied_resume || lead.resume));
      $("#cemail").value = (lead && lead.contact_email) || "";
      $("#cname").value = (lead && lead.contact_name) || "";
    }
    const captured = Boolean(lead && lead.jd);
    $(".capture").hidden = captured;
    $(".capture").disabled = !job.jd;
    $(".capture-hint").hidden = captured || Boolean(job.jd);
    $(".save").hidden = !lead;
    for (const id of ["#campaign", "#resume", "#cemail", "#cname"]) $(id).disabled = staged;
    $(".save").disabled = staged;

    // reach-out status + preview
    const email = check.data.email;
    $(".mail").hidden = !lead;
    $(".email-status").textContent = L.emailStatus(check.data);
    const showPreview = Boolean(email && email.subject);
    $(".preview").hidden = !showPreview;
    if (showPreview) {
      $(".subj").textContent = `To: ${email.to || "(no contact yet)"} · Subject: ${email.subject} · Attaches ${email.attachment}`;
      $(".body").textContent = email.body;
    }

    // application form tools
    const found = scanForm();
    $(".form").hidden = !found.length;
    $('.tab[data-tab="form"]').hidden = !found.length;
    $(".count").textContent = found.length ? ` ${found.length}` : "";
    if (!found.length && activeTab === "form") tabPicked = false;
    if (!tabPicked) showTab(found.length ? "form" : "posting");
    $(".attach").hidden = !resumeInput();
    const sig = [job.url, ...found.map((q) => q.label)].join("|"); // per posting: same form, other job = re-prepare
    if (found.length && sig !== preparedSig) await prepare(found, sig);

    $(".applied").hidden = staged;
    $(".applied").textContent = lead && lead.applied_resume ? "Applied ✓ (stage again)" : "Applied";
    if (L.isConfirmation(location.href, document.body.innerText) && !(lead && lead.applied_resume)) {
      status("Looks like you submitted. Click Applied to record it and stage the reach-out.");
    }
  }

  async function prepare(found, sig) {
    const profile = await send({ type: "profile" });
    if (!profile.ok) return status(profile.error, true);
    questions = found;
    preparedSig = sig;
    try {
      plan = L.planFill(questions, profile.data, lead && lead.answers);
    } catch (e) {
      plan = [];
      return status(e.message, true);
    }
    $(".rows").replaceChildren(...plan.map((p, i) => {
      const q = questions[i];
      const locked = p.source === "sensitive" || p.source === "eeo";
      const box = el("input", { type: "checkbox", checked: p.value != null, disabled: locked });
      const value = q.options && q.options.length
        ? el("select", { className: "v", disabled: locked }, new Option("—", ""),
             ...q.options.map((o) => new Option(o, o, false, o === p.value)))
        : el("input", { className: "v", value: p.value || "", disabled: locked });
      value.oninput = value.onchange = () => { box.checked = Boolean(value.value); };
      return el("div", { className: "row" },
        el("label", {}, box, " ", el("b", { textContent: L.displayLabel(p.label), title: p.label })),
        value, srcNote(p.source));
    }));
    const open = openQuestions().length;
    $(".ask").hidden = !open;
    $(".ask").textContent = `Ask Claude (${open})`;
  }

  const srcNote = (source) => {
    const note = el("div", { className: "src", textContent: SOURCE_NOTE[source] });
    note.dataset.src = String(source); // colours Claude / sensitive notes
    return note;
  };

  const SOURCE_NOTE = { claude: "Claude", bank: "answer bank", profile: "profile",
                        sensitive: "sensitive: fill it yourself", eeo: "self-ID: your call, never auto-filled",
                        null: "no answer yet" };

  const openQuestions = () => questions.filter((q, i) => plan[i] && plan[i].source === null)
    .map((q) => ({ label: q.label, kind: q.kind, options: q.options || [] }));

  async function askClaude() {
    const open = openQuestions();
    if (!open.length) return status("No unanswered questions.");
    const r = await send({ type: "questions", url: job.url, questions: open });
    r.ok ? status(`Sent ${open.length} question(s). In Claude Code, ask it to answer this application's questions, then click Re-check answers.`)
         : status(r.error, true);
  }

  onClick(".capture", async () => {
    const r = await send({ type: "capture", job: { ...job, ...details() } });
    if (!r.ok) return status(r.error, true);
    detailsDirty = false;
    status(r.data.created ? "Captured." : "Already captured; details saved.");
    safeRender();
  });

  onClick(".save", async () => {
    const r = await send({ type: "details", url: job.url, ...details() });
    if (!r.ok) return status(r.error, true);
    detailsDirty = false;
    status("Details saved.");
    safeRender();
  });

  onClick(".attach", async () => {
    const input = resumeInput();
    if (!input) return status("The résumé upload field is gone from the page.", true);
    const tag = $("#resume").value;
    if (!tag) return status("Pick a campaign and résumé first.", true);
    const [file, profile] = await Promise.all([send({ type: "resumeFile", tag }), send({ type: "profile" })]);
    if (!file.ok) return status(file.error, true);
    if (!profile.ok) return status(profile.error, true);
    const name = profile.data.fields.resume_filename || file.data.filename;
    const bytes = Uint8Array.from(atob(file.data.base64), (c) => c.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], name, { type: "application/pdf" }));
    input.files = dt.files;
    for (const t of ["input", "change"]) input.dispatchEvent(new Event(t, { bubbles: true }));
    attachedTag = tag;
    status(`Attached "${tag}" as ${name}.`);
  });

  onClick(".go", async () => {
    let filled = 0;
    root.querySelectorAll(".row").forEach((row, i) => {
      const box = row.querySelector("input[type=checkbox]");
      const value = row.querySelector(".v").value;
      if (box.checked && !box.disabled && value && fillOne(questions[i], value)) filled++;
    });
    status(`Filled ${filled} field(s). Check the form, then submit it yourself.`);
    if (autoAsk && openQuestions().length) await askClaude();
  });

  onClick(".ask", askClaude);
  onClick(".recheck", async () => { preparedSig = ""; await render(); status("Answers re-checked."); });

  async function markApplied(acknowledge) {
    const resume = $("#resume").value;
    if (!resume) return status("Pick the campaign and résumé you applied with.", true);
    if (attachedTag && attachedTag !== resume) {
      return status(`You attached "${attachedTag}" but the picker says "${resume}". Switch it back, or attach "${resume}" first.`, true);
    }
    const saveDetails = async () => {
      const saved = lead ? await send({ type: "details", url: job.url, ...details() })
                         : await send({ type: "capture", job: { ...job, ...details() } });
      if (saved.ok) detailsDirty = false;
      return saved;
    };
    if (detailsDirty && (lead || job.jd)) {
      const saved = await saveDetails();
      if (!saved.ok) return status(saved.error, true);
    }
    const apply = () => send({ type: "applied", url: job.url, resume, acknowledge_warnings: acknowledge });
    let r = await apply();
    if (!r.ok) return status(r.error, true);
    if (detailsDirty) { // applied before capturing: the lead exists now, so save details and stage
      lead = { lead_id: r.data.lead_id };
      const saved = await saveDetails();
      if (!saved.ok) return status(saved.error, true);
      r = await apply();
      if (!r.ok) return status(r.error, true);
    }
    const o = r.data.outreach;
    $(".confirm").hidden = o.status !== "needs_confirm";
    $(".anyway").hidden = o.status !== "needs_confirm";
    if (o.status === "needs_confirm") $(".confirm").textContent = `Before staging: ${o.warnings.join(" · ")}`;
    status({
      staged: `Applied with "${resume}". Reach-out staged to ${o.to}; it sends on the next runner.`,
      already_staged: `Already staged to ${o.to}.`,
      incomplete: `Applied with "${resume}". The reach-out stages once it has: ${(o.missing || []).join(", ")}.`,
      needs_confirm: "Applied. Check the warning above, then Stage anyway or leave it.",
      busy: `Applied with "${resume}". Not staged: ${o.reason}.`,
    }[o.status] || `Applied (${o.status}).`);
    safeRender();
  }

  onClick(".applied", () => markApplied(false));
  onClick(".anyway", () => markApplied(true));

  // After a submit click, single-page sites (Ashby, Workday) swap in their
  // confirmation without navigating: re-check for a minute.
  document.addEventListener("submit", () => {
    let n = 0;
    const t = setInterval(() => { safeRender(); if (++n >= 30) clearInterval(t); }, 2000);
  }, true);

  // SPA navigation (Workday pages): re-render when the URL changes.
  let last = location.href;
  setInterval(() => {
    if (location.href === last) return;
    const samePosting = L.canonicalJobUrl(location.href) === L.canonicalJobUrl(last);
    last = location.href;
    if (!samePosting) { detailsDirty = false; attachedTag = null; preparedSig = ""; } // nothing carries to another job
    safeRender();
  }, 1000);

  safeRender();
})();
