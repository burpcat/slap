// Run: node --test tests/extension/*.test.js   (content.test.js also needs: npm --prefix tests/extension install)
const test = require("node:test");
const assert = require("node:assert/strict");
const L = require("../../extension/lib.js");

test("canonicalJobUrl folds apply/confirmation steps into the posting URL", () => {
  assert.equal(L.canonicalJobUrl("https://jobs.lever.co/acme/abc-123/apply"), "https://jobs.lever.co/acme/abc-123");
  assert.equal(L.canonicalJobUrl("https://jobs.ashbyhq.com/acme/uuid/application?src=x#top"),
    "https://jobs.ashbyhq.com/acme/uuid");
  assert.equal(L.canonicalJobUrl("https://boards.greenhouse.io/acme/jobs/9/confirmation"),
    "https://boards.greenhouse.io/acme/jobs/9");
  assert.equal(L.canonicalJobUrl("https://x.com/"), "https://x.com/");
});

test("step pages keep their posting path, drop step queries, and never collapse to /", () => {
  assert.equal(L.canonicalJobUrl("https://jobs.lever.co/acme/abc/apply?lever-source=LinkedIn"),
    "https://jobs.lever.co/acme/abc");
  assert.equal(L.canonicalJobUrl("https://x.com/jobs/1/success"), "https://x.com/jobs/1");
  assert.equal(L.canonicalJobUrl("https://acme.wd1.myworkdayjobs.com/en-US/c/job/Boston/ML_R12/apply/applyManually"),
    "https://acme.wd1.myworkdayjobs.com/en-US/c/job/Boston/ML_R12");
  assert.equal(L.canonicalJobUrl("https://example.com/apply/eng-123"), "https://example.com/apply/eng-123");
  assert.notEqual(L.canonicalJobUrl("https://example.com/apply/eng-123"), L.canonicalJobUrl("https://example.com/apply/pm-456"));
  assert.equal(L.canonicalJobUrl("https://boards.greenhouse.io/acme/jobs/9?gh_jid=9"), "https://boards.greenhouse.io/acme/jobs/9?gh_jid=9");
});

test("isStepPage spots apply/confirmation steps only", () => {
  assert.ok(L.isStepPage("https://jobs.lever.co/acme/1/apply"));
  assert.ok(L.isStepPage("https://jobs.ashbyhq.com/acme/1/application"));
  assert.ok(!L.isStepPage("https://jobs.lever.co/acme/1"));
  assert.ok(!L.isStepPage("https://boards.greenhouse.io/acme/jobs/9"));
});

test("htmlToText keeps structure and decodes entities", () => {
  assert.equal(L.htmlToText("<p>Build &amp; ship</p><ul><li>LLMs</li><li>RAG&#39;s</li></ul>"),
    "Build & ship\n- LLMs\n- RAG's");
  // escaped markup, as Greenhouse puts it inside JSON-LD
  assert.equal(L.htmlToText("&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;&lt;ul&gt;&lt;li&gt;LLMs&lt;/li&gt;&lt;/ul&gt;"),
    "Build & ship\n- LLMs");
});

test("parseJobPosting finds JobPosting in arrays and @graph, skips junk", () => {
  const job = { "@type": "JobPosting", title: " ML Engineer ", hiringOrganization: { name: "Acme" },
    description: "<p>Agents</p>" };
  assert.deepEqual(L.parseJobPosting(["not json", JSON.stringify({ "@graph": [{ "@type": "Org" }, job] })]),
    { role: "ML Engineer", company: "Acme", jd: "Agents" });
  assert.deepEqual(L.parseJobPosting([JSON.stringify([{ ...job, "@type": ["Thing", "JobPosting"] }])]).company, "Acme");
  assert.equal(L.parseJobPosting([JSON.stringify({ ...job, description: "" })]), null);
  assert.equal(L.parseJobPosting([]), null);
});

test("displayLabel trims required marks and long help text", () => {
  assert.equal(L.displayLabel("Email*"), "Email");
  assert.equal(L.displayLabel("Why us? " + "x".repeat(120)), "Why us?");
  assert.equal(L.displayLabel("Your use of LinkedIn is entirely voluntary. " + "More text ".repeat(20)),
    "Your use of LinkedIn is entirely voluntary.");
});

test("parseJobPosting decodes escaped titles", () => {
  const job = { "@type": "JobPosting", title: "Architect, AI &amp; Agentic", hiringOrganization: { name: "A &amp; B" },
    description: "<p>x</p>" };
  assert.deepEqual(L.parseJobPosting([JSON.stringify(job)]), { role: "Architect, AI & Agentic", company: "A & B", jd: "x" });
});

test("isConfirmation by URL or page text", () => {
  assert.ok(L.isConfirmation("https://boards.greenhouse.io/acme/jobs/9/confirmation", ""));
  assert.ok(L.isConfirmation("https://jobs.lever.co/acme/1/thanks", ""));
  assert.ok(L.isConfirmation("https://jobs.ashbyhq.com/acme/1", "Your application has been submitted!"));
  assert.ok(!L.isConfirmation("https://jobs.lever.co/acme/1/apply", "Submit your application"));
});

test("badgeLines: posting state plus prior contacts", () => {
  assert.deepEqual(L.badgeLines({ lead: null, company_contacts: [] }), ["No prior contact at this company"]);
  assert.deepEqual(L.badgeLines({ lead: { jd: "x", staged_to: [] },
    company_contacts: [{ recipient: "jane@acme.com", status: "active", replied_at: "t" }] }),
    ["Captured", "Already contacted jane@acme.com (active, replied)"]);
  assert.deepEqual(L.badgeLines({ lead: { applied_resume: "b", staged_to: [] }, company_contacts: [] }),
    ['Applied with "b"', "No prior contact at this company"]);
  assert.equal(L.badgeLines({ lead: { staged_to: ["hm@x.com"] }, company_contacts: [] })[0], "Reach-out staged to hm@x.com");
});

test("emailStatus says what the reach-out waits for", () => {
  const lead = (extra) => ({ campaign: "hm", staged_to: [], applied_resume: null, ...extra });
  assert.equal(L.emailStatus({ lead: null }), "");
  assert.match(L.emailStatus({ lead: lead({ campaign: "" }) }), /Pick a campaign/);
  assert.match(L.emailStatus({ lead: lead(), email: { missing: ["contact email", "why_line"] } }), /contact's email/);
  assert.match(L.emailStatus({ lead: lead(), email: { missing: ["why_line"] } }), /write the drop/);
  assert.match(L.emailStatus({ lead: lead(), email: { missing: [] } }), /stages when you click Applied/);
  assert.match(L.emailStatus({ lead: lead({ staged_to: ["a@b.co"] }) }), /staged to a@b.co/);
});

const PROFILE = {
  fields: { first_name: "Ada", last_name: "Lovelace", email: "ada@x.com", work_authorization: "Yes",
            sponsorship: "No", linkedin: "https://linkedin.com/in/ada" },
  answers: [{ match: "notice period|start", answer: "Two weeks" }],
};

test("matchOption: exact, then prefix either way, else null", () => {
  assert.equal(L.matchOption(["Yes", "No"], "yes"), "Yes");
  assert.equal(L.matchOption(["Yes, I am authorized", "No"], "Yes"), "Yes, I am authorized");
  assert.equal(L.matchOption(["0-2", "3-5"], "10+"), null);
  assert.equal(L.matchOption(["No", "Yes"], "None"), null);            // never the reverse prefix
  assert.equal(L.matchOption(["No", "Yes"], "Not required"), null);
});

test("planFill precedence and exclusions", () => {
  const plan = L.planFill([
    { label: "First Name" }, { label: "Full name" }, { label: "Email*" },
    { label: "Will you require visa sponsorship?", options: ["Yes", "No"] },
    { label: "Are you legally authorized to work in the US?", options: ["Yes", "No"] },
    { label: "What is your notice period?" },
    { label: "Why Acme?" },
    { label: "Gender", options: ["Woman", "Man"] },
    { label: "Social Security Number" },
    { label: "Years of Python", options: ["0-2", "3-5"] },
  ], PROFILE, { "Why Acme?": "Agents.", "Years of Python": "10+" });
  const by = Object.fromEntries(plan.map((p) => [p.label, [p.value, p.source]]));
  assert.deepEqual(by["First Name"], ["Ada", "profile"]);
  assert.deepEqual(by["Full name"], ["Ada Lovelace", "profile"]);
  assert.deepEqual(by["Email*"], ["ada@x.com", "profile"]);
  assert.deepEqual(by["Will you require visa sponsorship?"], ["No", "profile"]);
  assert.deepEqual(by["Are you legally authorized to work in the US?"], ["Yes", "profile"]);
  assert.deepEqual(by["What is your notice period?"], ["Two weeks", "bank"]);
  assert.deepEqual(by["Why Acme?"], ["Agents.", "claude"]);
  assert.deepEqual(by["Gender"], [null, "eeo"]);
  assert.deepEqual(by["Social Security Number"], [null, "sensitive"]);
  assert.deepEqual(by["Years of Python"], [null, null]); // Claude's answer fits no option: left for you/Claude
});

test("planFill: self-ID by option text or broader label; bad bank regex fails loud", () => {
  const plan = L.planFill([
    { label: "question_123", options: ["Yes", "No", "I decline to self-identify"] },
    { label: "Sex", options: ["F", "M"] },
    { label: "Religion" },
  ], PROFILE, {});
  assert.deepEqual(plan.map((p) => p.source), ["eeo", "eeo", "eeo"]);
  assert.throws(() => L.planFill([{ label: "x" }], { answers: [{ match: "(?P<n>x)", answer: "y" }] }),
    /answers\[0\]\.match .* not a valid JavaScript regex/);
});
