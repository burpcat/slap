// End-to-end DOM check of content.js in jsdom with a stubbed background.
// Run: npm --prefix tests/extension install && node --test tests/extension/*.test.js
const test = require("node:test");
const fs = require("fs");
const assert = require("assert/strict");
const { JSDOM } = require("jsdom");
const EXT = __dirname + "/../../extension/";

const html = `<!doctype html><html><body>
<script type="application/ld+json">${JSON.stringify({"@type":"JobPosting",title:"ML Engineer",hiringOrganization:{name:"Acme"},description:"<p>"+"Build agents. ".repeat(30)+"</p>"})}</script>
<form id="app">
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
  <label for="cv">Resume/CV</label><input id="cv" type="file" name="resume">
</form></body></html>`;

const dom = new JSDOM(html, { url: "https://boards.greenhouse.io/acme/jobs/9", runScripts: "outside-only", pretendToBeVisual: true });
const w = dom.window;
// jsdom has no layout: treat everything as visible.
Object.defineProperty(w.HTMLElement.prototype, "offsetParent", { get() { return this.parentNode; } });
w.CSS = { escape: (s) => String(s).replace(/["\\]/g, "\\$&") }; // jsdom gap; Chrome has CSS.escape
const sent = [];
const replies = {
  rememberJob: { ok: true }, recallJob: { ok: true, data: null },
  check: { ok: true, data: { lead: { lead_id: "L", jd: "x", resume: "ai", campaign: "hiring-manager", answers: { "Why Acme?": "Your agent platform." } }, company_contacts: [] } },
  resumes: { ok: true, data: { resumes: [{ tag: "ai", file: "ai.pdf" }, { tag: "ds", file: "ds.pdf" }] } },
  profile: { ok: true, data: { fields: { first_name: "Ada", email: "ada@x.com", phone: "555", sponsorship: "No", work_authorization: "Yes" }, answers: [] } },
  questions: { ok: true, data: { lead_id: "L" } },
};
w.chrome = { runtime: { sendMessage: async (m) => { sent.push(m); return replies[m.type]; } } };
// React-style controlled input tracking: count native-setter writes seen by an 'input' listener.
let inputEvents = 0;
w.document.getElementById("fn").addEventListener("input", () => inputEvents++);

const intervals = [];
const realSetInterval = w.setInterval.bind(w);
w.setInterval = (fn, ms) => { const id = realSetInterval(fn, ms); intervals.push(id); return id; };
// The panel's shadow root is closed; capture it for the test.
let panelRoot = null;
const realAttach = w.Element.prototype.attachShadow;
w.Element.prototype.attachShadow = function (init) { return (panelRoot = realAttach.call(this, init)); };
w.eval(fs.readFileSync(EXT + "lib.js", "utf8"));
w.eval(fs.readFileSync(EXT + "content.js", "utf8"));
const tick = () => new Promise((r) => setTimeout(r, 50));

test("panel: extract, badge, prepare, fill, ask Claude, résumé pick persists", async () => {

  await tick();
  assert.equal(w.document.documentElement.lastElementChild.shadowRoot, null); // closed to the page
  const root = panelRoot;
  const $ = (s) => root.querySelector(s);

  // A page script's synthetic click is ignored: nothing gets prepared or filled.
  $(".prep").click(); await tick();
  assert.equal(root.querySelectorAll(".row").length, 0);
  w.__slapTrustSyntheticClicksForTests = true; // jsdom can't make trusted clicks
  assert.equal($(".job").textContent, "Acme · ML Engineer");
  assert.match($(".lines").textContent, /Matched résumé: "ai"/);
  assert.equal($(".res select").value, "ai");
  assert.equal($(".capture").hidden, true);          // lead already has a JD
  assert.equal($(".attach").hidden, false);          // résumé file input found
  assert.equal($(".fill").hidden, false);

  $(".prep").click(); await tick();
  const rows = [...root.querySelectorAll(".row")].map((r) => [r.querySelector("b").textContent, r.querySelector(".v").value, r.querySelector(".src").textContent, r.querySelector("input[type=checkbox]").checked]);
  const by = Object.fromEntries(rows.map(([l, v, s, c]) => [l, [v, s, c]]));
  assert.deepEqual(by["First Name*"], ["Ada", "profile", true]);
  assert.deepEqual(by["Email"], ["ada@x.com", "profile", true]);
  assert.deepEqual(by["Phone"], ["555", "profile", true]);
  assert.deepEqual(by["Why Acme?"], ["Your agent platform.", "Claude", true]);
  assert.deepEqual(by["Will you require visa sponsorship?"], ["No", "profile", true]);
  assert.deepEqual(by["Are you legally authorized to work in the US?"], ["Yes", "profile", true]);
  assert.equal(by["Gender"][1], "self-ID: your call, never auto-filled");
  assert.deepEqual(by["Describe a hard bug you fixed"], ["", "no answer yet", false]);
  assert.equal($(".ask").textContent, "Ask Claude (1)");

  $(".go").click(); await tick();
  const d = w.document;
  assert.equal(d.getElementById("fn").value, "Ada");
  assert.ok(inputEvents >= 1, "input event fired for controlled inputs");
  assert.equal(d.querySelector("[name=email]").value, "ada@x.com");
  assert.equal(d.getElementById("why").value, "Your agent platform.");
  assert.equal(d.getElementById("spon").value, "0");
  assert.equal(d.querySelector("input[name=auth][value=y]").checked, true);
  assert.equal(d.getElementById("g").value, "");      // EEO untouched
  assert.equal(d.getElementById("exp").value, "");    // unanswered untouched

  $(".ask").click(); await tick();
  const q = sent.find((m) => m.type === "questions");
  assert.deepEqual(JSON.parse(JSON.stringify(q.questions)), [{ label: "Describe a hard bug you fixed", kind: "textarea", options: [] }]);
  assert.equal(q.url, "https://boards.greenhouse.io/acme/jobs/9");

  // résumé pick survives a re-render
  const sel = $(".res select"); sel.value = "ds"; sel.dispatchEvent(new w.Event("change"));
  w.document.dispatchEvent(new w.Event("submit")); await new Promise((r) => setTimeout(r, 2200));
  assert.equal($(".res select").value, "ds");
  for (const id of intervals) w.clearInterval(id);
});
