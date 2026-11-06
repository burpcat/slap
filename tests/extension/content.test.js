// End-to-end DOM check of content.js in jsdom with a stubbed background.
// Run: npm --prefix tests/extension install && node --test --test-force-exit tests/extension/*.test.js
const test = require("node:test");
const fs = require("fs");
const assert = require("assert/strict");
const { JSDOM } = require("jsdom");
const EXT = __dirname + "/../../extension/";

const JOB_LD = `<script type="application/ld+json">${JSON.stringify({ "@type": "JobPosting", title: "ML Engineer",
  hiringOrganization: { name: "Acme" }, description: "<p>" + "Build agents. ".repeat(30) + "</p>" })}</script>`;

const FORM = `<form id="app">
  <label for="fn">First Name*</label><input id="fn" name="first_name">
  <label>Email <input name="email" type="email"></label>
  <input aria-label="Phone" name="phone" type="tel">
  <label for="why">Why Acme?</label><textarea id="why"></textarea>
  <label for="spon">Will you require visa sponsorship?</label>
  <select id="spon"><option value="">Select...</option><option value="1">Yes</option><option value="0">No</option></select>
  <fieldset><legend>Are you legally authorized to work in the US?</legend>
    <label><input type="radio" name="auth" value="y">Yes</label><label><input type="radio" name="auth" value="n">No</label></fieldset>
  <label for="g">Gender</label><select id="g"><option value="">-</option><option value="w">Woman</option></select>
  <label for="exp">Describe a hard bug you fixed</label><textarea id="exp"></textarea>
  <div class="ashby-application-form-field-entry"><label class="ashby-label">Location</label>
    <div><input name="loc" placeholder="Start typing..."></div></div>
  <label for="cv">Resume/CV</label><input id="cv" type="file" name="resume">
</form>`;

const LEAD = { lead_id: "L", jd: "x", campaign: "test-hm", resume: "resume-a", contact_email: "sam@acme.com",
  contact_name: "Sam", applied_resume: null, staged_to: [], answers: { "Why Acme?": "Your agent platform." } };

function boot(html, url, replies) {
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`,
    { url, runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window;
  Object.defineProperty(w.HTMLElement.prototype, "offsetParent", { get() { return this.parentNode; } }); // no layout in jsdom
  w.CSS = { escape: (s) => String(s).replace(/["\\]/g, "\\$&") }; // jsdom gap; Chrome has CSS.escape
  const sent = [];
  const listeners = [];
  const store = {};
  w.chrome = {
    runtime: { sendMessage: async (m) => { sent.push(m); return typeof replies[m.type] === "function" ? replies[m.type](m) : replies[m.type]; },
               onMessage: { addListener: (fn) => listeners.push(fn) } },
    storage: { local: { get: async (k) => ({ [k]: store[k] }), set: async (o) => Object.assign(store, o) } },
  };
  const intervals = [];
  const realSetInterval = w.setInterval.bind(w);
  w.setInterval = (fn, ms) => { const id = realSetInterval(fn, ms); intervals.push(id); return id; };
  let root = null; // the panel's shadow root is closed; capture it for the test
  const realAttach = w.Element.prototype.attachShadow;
  w.Element.prototype.attachShadow = function (init) { return (root = realAttach.call(this, init)); };
  w.eval(fs.readFileSync(EXT + "lib.js", "utf8"));
  w.eval(fs.readFileSync(EXT + "content.js", "utf8"));
  return { w, sent, listeners, root: () => root, stop: () => intervals.forEach((id) => w.clearInterval(id)) };
}

const tick = () => new Promise((r) => setTimeout(r, 60));
const baseReplies = (lead) => ({
  rememberJob: { ok: true }, recallJob: { ok: true, data: null },
  check: { ok: true, data: { lead, company_contacts: [], email_warnings: [],
    email: lead && lead.campaign ? { to: lead.contact_email, subject: "Your ml engineer opening at Acme", body: "Hi Sam,\n...",
      attachment: "Resume.pdf (résumé: resume-a)", missing: [] } : null } },
  campaignOptions: { ok: true, data: { campaigns: [{ name: "test-hm", resumes: ["resume-a", "resume-b"] },
                                                   { name: "test-rec", resumes: ["resume-b"] }] } },
  profile: { ok: true, data: { fields: { first_name: "Ada", email: "ada@x.com", phone: "555", sponsorship: "No",
    work_authorization: "Yes" }, answers: [] } },
  questions: { ok: true, data: { lead_id: "L" } },
  details: { ok: true, data: { lead_id: "L" } },
  saveAnswer: (m) => ({ ok: true, data: { saved: { match: m.match, answer: m.answer } } }),
  capture: { ok: true, data: { lead_id: "L", created: true } },
  applied: { ok: true, data: { lead_id: "L", outreach: { status: "staged", to: "sam@acme.com" } } },
});

test("application page: answers prepared on load, fill, ask Claude, Applied stages the reach-out", async () => {
  const t = boot(JOB_LD + FORM, "https://boards.greenhouse.io/acme/jobs/9", baseReplies(LEAD));
  try {
    await tick();
    const root = t.root();
    const $ = (s) => root.querySelector(s);
    assert.equal(t.w.document.documentElement.lastElementChild.shadowRoot, null); // closed to the page
    assert.equal($(".job").textContent, "Acme · ML Engineer");
    assert.equal($("#campaign").value, "test-hm");
    assert.equal($("#resume").value, "resume-a");
    assert.equal($("#cemail").value, "sam@acme.com");
    assert.equal($(".capture").hidden, true);              // already captured
    assert.match($(".email-status").textContent, /stages when you click Applied/);
    assert.equal($(".preview").hidden, false);
    assert.match($(".subj").textContent, /Your ml engineer opening at Acme/);

    // prepared automatically, no click needed
    const rows = [...root.querySelectorAll(".row")].map((r) => [r.querySelector("b").title, r.querySelector(".v").value,
      r.querySelector(".src").textContent, r.querySelector("input[type=checkbox]").checked]);
    const by = Object.fromEntries(rows.map(([l, v, s, c]) => [l, [v, s, c]]));
    assert.deepEqual(by["First Name*"], ["Ada", "profile", true]);
    assert.deepEqual(by["Why Acme?"], ["Your agent platform.", "Claude", true]);
    assert.deepEqual(by["Will you require visa sponsorship?"], ["No", "profile", true]);
    assert.equal(by["Gender"][1], "self-ID: your call, never auto-filled");
    assert.ok("Location" in by && !("Start typing..." in by));
    assert.equal($(".ask").textContent, "Ask Claude (2)");

    // a page script's synthetic click does nothing
    $(".go").click(); await tick();
    assert.equal(t.w.document.getElementById("fn").value, "");
    t.w.__slapTrustSyntheticClicksForTests = true; // jsdom can't make trusted clicks

    $(".go").click(); await tick();
    const d = t.w.document;
    assert.equal(d.getElementById("fn").value, "Ada");
    assert.equal(d.getElementById("why").value, "Your agent platform.");
    assert.equal(d.getElementById("spon").value, "0");
    assert.equal(d.querySelector("input[name=auth][value=y]").checked, true);
    assert.equal(d.getElementById("g").value, "");
    assert.equal(t.sent.filter((m) => m.type === "questions").length, 0); // auto-send is off

    $(".ask").click(); await tick();
    const q = t.sent.find((m) => m.type === "questions");
    assert.deepEqual(JSON.parse(JSON.stringify(q.questions)).map((x) => x.label), ["Describe a hard bug you fixed", "Location"]);

    // Save to bank: hidden on an unedited profile row, offered once you type an answer
    const rowFor = (label) => [...root.querySelectorAll(".row")].find((r) => r.querySelector("b").title === label);
    assert.equal(rowFor("First Name*").querySelector(".learn").hidden, true);
    const loc = rowFor("Location");
    assert.equal(loc.querySelector(".learn").hidden, true);              // empty
    const v = loc.querySelector(".v");
    v.value = "Boston, MA"; v.dispatchEvent(new t.w.Event("input"));
    assert.equal(loc.querySelector(".learn").hidden, false);
    assert.equal(rowFor("Why Acme?").querySelector(".learn").hidden, false); // Claude's answer
    loc.querySelector(".learn").click();
    assert.equal(loc.querySelector(".phrase").value, "location");
    loc.querySelector(".learnbox .primary").click(); await tick();
    assert.deepEqual(JSON.parse(JSON.stringify(t.sent.find((m) => m.type === "saveAnswer"))),
      { type: "saveAnswer", match: "location", answer: "Boston, MA" });
    assert.equal(loc.querySelector(".src").textContent, "answer bank · saved");
    assert.equal(loc.querySelector(".learnbox"), null);

    $(".applied").click(); await tick();
    const a = t.sent.find((m) => m.type === "applied");
    assert.equal(a.resume, "resume-a");
    assert.match($(".status").textContent, /Reach-out staged to sam@acme.com/);

    const host = d.documentElement.lastElementChild;
    $(".x").click();
    assert.equal(host.style.display, "none");
    t.listeners.forEach((fn) => fn({ type: "show" }));
    assert.equal(host.style.display, "");
  } finally {
    t.stop();
  }
});

test("posting page: capture sends the JD with campaign, résumé and contact; auto-send fires after fill", async () => {
  const replies = baseReplies(null);
  const t = boot(JOB_LD + FORM, "https://jobs.lever.co/acme/abc", replies);
  try {
    await tick();
    t.w.__slapTrustSyntheticClicksForTests = true;
    const root = t.root();
    const $ = (s) => root.querySelector(s);
    assert.equal($(".capture").hidden, false);
    assert.equal($(".mail").hidden, true);

    const pick = (sel, value, evt = "change") => { $(sel).value = value; $(sel).dispatchEvent(new t.w.Event(evt)); };
    pick("#campaign", "test-rec");
    assert.deepEqual([...$("#resume").options].map((o) => o.value), ["resume-b"]); // résumés follow the campaign
    pick("#cemail", "hm@acme.com", "input");
    pick("#cname", "Pat", "input");
    $(".capture").click(); await tick();
    const c = t.sent.find((m) => m.type === "capture");
    assert.equal(c.job.url, "https://jobs.lever.co/acme/abc");
    assert.equal(c.job.company, "Acme");
    assert.ok(c.job.jd.startsWith("Build agents."));
    assert.deepEqual([c.job.campaign, c.job.resume, c.job.contact_email, c.job.contact_name],
      ["test-rec", "resume-b", "hm@acme.com", "Pat"]);

    const auto = $(".auto");
    auto.checked = true; auto.dispatchEvent(new t.w.Event("change"));
    $(".go").click(); await tick();
    assert.equal(t.sent.filter((m) => m.type === "questions").length, 1); // queued without a click
  } finally {
    t.stop();
  }
});
