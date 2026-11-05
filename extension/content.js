// On-page panel: capture this posting into slap, show prior contact + the
// matched résumé, prepare form answers for your review, attach the résumé,
// and record the application once you've submitted. Never clicks Submit and
// never fills or records anything without your click.
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
         border-radius:10px;box-shadow:0 4px 18px rgba(0,0,0,.15);padding:10px 12px;width:320px;
         max-height:70vh;overflow:auto}
      .h{display:flex;justify-content:space-between;font-weight:600;margin-bottom:4px}
      .job{color:#555;margin-bottom:6px}.lines div{margin:2px 0}.err{color:#b00020}
      .confirm{background:#fff6d6;border-radius:6px;padding:6px;margin:6px 0}
      button,select,input{font:inherit}button{cursor:pointer;margin-top:6px}
      .x{border:0;background:none;color:#888;margin:0}
      .row{border-top:1px solid #eee;padding:4px 0}.row b{font-weight:500}.row input.v,.row select.v{width:100%;box-sizing:border-box}
      .src{color:#888;font-size:11px}
      @media (prefers-color-scheme:dark){.p{background:#1f1f1f;color:#eee;border-color:#444}.job{color:#aaa}
        .confirm{background:#3a3110}.row{border-color:#333}}
    </style>
    <div class="p">
      <div class="h"><span>slap</span><button class="x" title="Hide">✕</button></div>
      <div class="job"></div><div class="lines"></div>
      <div class="confirm" hidden>Looks like you submitted. Record it so the held email can go out.</div>
      <div><button class="capture" hidden>Send to slap</button></div>
      <div class="res" hidden>
        <select aria-label="Résumé"></select>
        <button class="attach" hidden>Attach to form</button>
        <button class="mark">Mark applied</button>
      </div>
      <div class="fill" hidden>
        <button class="prep">Prepare answers</button>
        <div class="rows"></div>
        <button class="go" hidden>Fill checked</button>
        <button class="ask" hidden></button>
      </div>
      <div class="status"></div>
    </div>`;
  const $ = (sel) => root.querySelector(sel);
  // Only a real click from you counts. A page script calling .click() makes an
  // untrusted event, and filling a form or recording an application (which can
  // release a held email) must never happen on the page's say-so. The test hook
  // is safe in Chrome: content scripts run in an isolated world, so the page
  // can't set globals the extension reads.
  const onClick = (sel, fn) => {
    $(sel).onclick = (e) => {
      if (!e.isTrusted && globalThis.__slapTrustSyntheticClicksForTests !== true) return;
      Promise.resolve(fn(e)).catch((err) => status(String(err.message || err), true));
    };
  };
  onClick(".x", () => { host.style.display = "none"; });
  // The toolbar icon brings a closed panel back (background.js sends "show").
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "show") { host.style.display = ""; safeRender(); }
  });
  document.documentElement.appendChild(host);

  const status = (text, isErr) => { $(".status").textContent = text || ""; $(".status").className = isErr ? "status err" : "status"; };
  const el = (tag, props, ...kids) => { const e = Object.assign(document.createElement(tag), props); e.append(...kids); return e; };

  let job = null;
  let lead = null;
  let chosenTag = null; // your pick in the résumé select survives re-renders
  let questions = [];
  let plan = [];
  const safeRender = () => render().catch((e) => status(String(e), true));
  $(".res select").onchange = (e) => { chosenTag = e.target.value; };

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

    const [check, resumes] = await Promise.all([
      send({ type: "check", company: job.company, url: job.url }), send({ type: "resumes" })]);
    if (!check.ok) return status(check.error, true);
    lead = check.data.lead;
    const lines = L.badgeLines(check.data);
    if (!page && !lead) lines.unshift(`Not tied to a captured posting: anything you record here goes to ${job.url}`);
    $(".lines").replaceChildren(...lines.map((t) => el("div", { textContent: t })));
    if (!resumes.ok) status(resumes.error, true);

    $(".capture").hidden = Boolean(lead && lead.jd) || !job.jd;
    const tags = resumes.ok ? resumes.data.resumes.map((r) => r.tag) : [];
    const preferred = chosenTag || (lead && (lead.applied_resume || lead.resume));
    $(".res select").replaceChildren(...tags.map((t) => new Option(t, t, false, t === preferred)));
    $(".res").hidden = !tags.length;
    $(".mark").hidden = Boolean(lead && lead.applied_resume);
    $(".attach").hidden = !resumeInput();
    $(".fill").hidden = !scanForm().length;
    $(".confirm").hidden = !(L.isConfirmation(location.href, document.body.innerText) && !(lead && lead.applied_resume));
  }

  onClick(".capture", async () => {
    const r = await send({ type: "capture", job });
    r.ok ? status(r.data.created ? "Captured. Run slap_leads in Claude Code to match and draft." : "Already captured.")
         : status(r.error, true);
    safeRender();
  });

  onClick(".mark", async () => {
    const resume = $(".res select").value;
    const r = await send({ type: "applied", url: job.url, resume });
    r.ok ? status(`Recorded: applied with "${resume}".`) : status(r.error, true);
    safeRender();
  });

  onClick(".attach", async () => {
    const input = resumeInput();
    if (!input) return status("The résumé upload field is gone from the page.", true);
    const tag = $(".res select").value;
    const [file, profile] = await Promise.all([send({ type: "resumeFile", tag }), send({ type: "profile" })]);
    if (!file.ok) return status(file.error, true);
    if (!profile.ok) return status(profile.error, true);
    const name = profile.data.fields.resume_filename || file.data.filename;
    const bytes = Uint8Array.from(atob(file.data.base64), (c) => c.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], name, { type: "application/pdf" }));
    input.files = dt.files;
    for (const t of ["input", "change"]) input.dispatchEvent(new Event(t, { bubbles: true }));
    chosenTag = tag;
    status(`Attached "${tag}" as ${name}. Mark applied with the same résumé after you submit.`);
  });

  const SOURCE_NOTE = { claude: "Claude", bank: "answer bank", profile: "profile",
                        sensitive: "sensitive: fill it yourself", eeo: "self-ID: your call, never auto-filled",
                        null: "no answer yet" };

  onClick(".prep", async () => {
    await render(); // fresh lead: picks up answers Claude saved since the last render
    const profile = await send({ type: "profile" });
    if (!profile.ok) return status(profile.error, true);
    questions = scanForm();
    plan = L.planFill(questions, profile.data, lead && lead.answers);
    $(".rows").replaceChildren(...plan.map((p, i) => {
      const q = questions[i];
      const locked = p.source === "sensitive" || p.source === "eeo";
      const box = el("input", { type: "checkbox", checked: p.value != null, disabled: locked });
      const value = q.options && q.options.length
        ? el("select", { className: "v", disabled: locked }, new Option("—", ""),
             ...q.options.map((o) => new Option(o, o, false, o === p.value)))
        : el("input", { className: "v", value: p.value || "", disabled: locked });
      value.oninput = value.onchange = () => { box.checked = Boolean(value.value); };
      return el("div", { className: "row" }, el("label", {}, box, " ", el("b", { textContent: L.displayLabel(p.label), title: p.label })),
                value, el("div", { className: "src", textContent: SOURCE_NOTE[p.source] }));
    }));
    const open = plan.filter((p) => p.source === null).length;
    $(".go").hidden = !plan.length;
    $(".ask").hidden = !open || !job.url;
    $(".ask").textContent = `Ask Claude (${open})`;
    status("Review every value, then Fill checked. You submit the form yourself.");
  });

  onClick(".go", () => {
    let filled = 0;
    root.querySelectorAll(".row").forEach((row, i) => {
      const box = row.querySelector("input[type=checkbox]");
      const value = row.querySelector(".v").value;
      if (box.checked && !box.disabled && value && fillOne(questions[i], value)) filled++;
    });
    status(`Filled ${filled} field(s). Check the form, then submit it yourself.`);
  });

  onClick(".ask", async () => {
    const open = questions.filter((q, i) => plan[i].source === null)
      .map((q) => ({ label: q.label, kind: q.kind, options: q.options || [] }));
    const r = await send({ type: "questions", url: job.url, questions: open });
    r.ok ? status(`Sent ${open.length} question(s). In Claude Code, run slap_pending_questions, then Prepare answers again.`)
         : status(r.error, true);
  });

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
