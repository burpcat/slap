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
  root.innerHTML = `
    <style>
      .p{font:13px/1.4 system-ui,sans-serif;background:#fff;color:#1a1a1a;border:1px solid #d0d0d0;
         border-radius:10px;box-shadow:0 4px 18px rgba(0,0,0,.15);padding:10px 12px;width:330px;
         max-height:75vh;overflow:auto;display:flex;flex-direction:column;gap:8px}
      .h{display:flex;justify-content:space-between;font-weight:600}
      .job{color:#555}.lines div{margin:1px 0}.err{color:#b00020}.muted{color:#777;font-size:12px}
      .box{border-top:1px solid #e3e3e3;padding-top:8px;display:flex;flex-direction:column;gap:6px}
      .grid{display:grid;grid-template-columns:auto 1fr;gap:4px 8px;align-items:center}
      .grid input,.grid select{width:100%;box-sizing:border-box}
      .confirm{background:#fff6d6;border-radius:6px;padding:6px}
      button,select,input{font:inherit}button{cursor:pointer;align-self:flex-start}
      .x{border:0;background:none;color:#888}
      .row{border-top:1px solid #eee;padding:4px 0}.row b{font-weight:500}
      .row input.v,.row select.v{width:100%;box-sizing:border-box}.src{color:#888;font-size:11px}
      pre{white-space:pre-wrap;font:12px/1.4 ui-monospace,Menlo,monospace;margin:4px 0 0}
      @media (prefers-color-scheme:dark){.p{background:#1f1f1f;color:#eee;border-color:#444}.job{color:#aaa}
        .confirm{background:#3a3110}.row,.box{border-color:#333}}
    </style>
    <div class="p">
      <div class="h"><span>slap</span><button class="x" title="Hide (the toolbar icon brings it back)">✕</button></div>
      <div class="job"></div>
      <div class="lines"></div>

      <div class="box setup">
        <div class="grid">
          <label for="campaign">Campaign</label><select id="campaign"></select>
          <label for="resume">Résumé</label><select id="resume"></select>
          <label for="cemail">Contact email</label><input id="cemail" type="email" placeholder="who gets the reach-out">
          <label for="cname">Contact name</label><input id="cname" placeholder="first name">
        </div>
        <div><button class="capture">Capture</button> <button class="save" hidden>Save details</button></div>
        <div class="muted capture-hint" hidden>Open the job posting page to capture it.</div>
      </div>

      <div class="box mail" hidden>
        <div class="email-status"></div>
        <details class="preview" hidden><summary>Preview email</summary><div class="subj"></div><pre class="body"></pre></details>
      </div>

      <div class="box form" hidden>
        <div><button class="attach" hidden>Attach résumé</button></div>
        <label class="muted"><input type="checkbox" class="auto"> Auto-send unanswered questions to Claude after filling</label>
        <div class="rows"></div>
        <div><button class="go">Fill checked</button> <button class="ask" hidden></button> <button class="recheck">Re-check answers</button></div>
      </div>

      <div class="box applied-box">
        <div class="confirm" hidden></div>
        <div><button class="applied">Applied</button> <button class="anyway" hidden>Stage anyway</button></div>
      </div>
      <div class="status"></div>
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

    const lines = L.badgeLines(check.data);
    if (!page && !lead) lines.unshift(`Not tied to a captured posting: anything you record here goes to ${job.url}`);
    $(".lines").replaceChildren(...lines.map((t) => el("div", { textContent: t })));

    // setup: campaign / résumé / contact (left alone while you're editing)
    const staged = Boolean(lead && lead.staged_to && lead.staged_to.length);
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
    $(".attach").hidden = !resumeInput();
    const sig = found.map((q) => q.label).join("|");
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
        value, el("div", { className: "src", textContent: SOURCE_NOTE[p.source] }));
    }));
    const open = openQuestions().length;
    $(".ask").hidden = !open;
    $(".ask").textContent = `Ask Claude (${open})`;
  }

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
  setInterval(() => { if (location.href !== last) { last = location.href; safeRender(); } }, 1000);

  safeRender();
})();
