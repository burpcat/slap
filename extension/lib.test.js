// Run: node --test extension/*.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const L = require("./lib.js");

test("canonicalJobUrl folds apply/confirmation steps into the posting URL", () => {
  assert.equal(L.canonicalJobUrl("https://jobs.lever.co/acme/abc-123/apply"), "https://jobs.lever.co/acme/abc-123");
  assert.equal(L.canonicalJobUrl("https://jobs.ashbyhq.com/acme/uuid/application?src=x#top"),
    "https://jobs.ashbyhq.com/acme/uuid?src=x");
  assert.equal(L.canonicalJobUrl("https://boards.greenhouse.io/acme/jobs/9/confirmation"),
    "https://boards.greenhouse.io/acme/jobs/9");
  assert.equal(L.canonicalJobUrl("https://x.com/"), "https://x.com/");
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

test("isConfirmation by URL or page text", () => {
  assert.ok(L.isConfirmation("https://boards.greenhouse.io/acme/jobs/9/confirmation", ""));
  assert.ok(L.isConfirmation("https://jobs.lever.co/acme/1/thanks", ""));
  assert.ok(L.isConfirmation("https://jobs.ashbyhq.com/acme/1", "Your application has been submitted!"));
  assert.ok(!L.isConfirmation("https://jobs.lever.co/acme/1/apply", "Submit your application"));
});

test("badgeLines summarizes lead state and prior contacts", () => {
  assert.deepEqual(L.badgeLines({ lead: null, company_contacts: [] }), ["No prior contact found"]);
  assert.deepEqual(L.badgeLines({ lead: { resume: "ai", campaign: "hiring-manager" },
    company_contacts: [{ recipient: "jane@acme.com", status: "active", replied_at: "t" }] }),
    ['Matched résumé: "ai" (hiring-manager)', "Already contacted jane@acme.com (active, replied)"]);
  assert.deepEqual(L.badgeLines({ lead: { resume: "ai", applied_resume: "ds" }, company_contacts: [] }),
    ['Applied with "ds"']);
});
