// Pure, DOM-free helpers shared by content.js and the node tests (lib.test.js).
(function (root) {
  "use strict";

  // A step of a posting (apply form, confirmation), not a different job. The
  // posting's own path must come first, so /apply/eng-123 (a site whose job
  // pages live under /apply) is NOT mistaken for a step of "/".
  // Keep in sync with slap/leads.py STEP_TAIL.
  const STEP = /^(\/.*[^/].*?)\/(apply|application|confirmation|thanks|thank-you|submitted|success)(\/.*)?$/i;

  /** Is this an apply/confirmation step of a posting rather than the posting itself? */
  function isStepPage(href) {
    return STEP.test(new URL(href).pathname);
  }

  /** One URL per posting, so the apply/confirmation pages find the captured lead.
   *  A step page drops its query too (?lever-source=..., tracking). */
  function canonicalJobUrl(href) {
    const u = new URL(href);
    u.hash = "";
    const m = u.pathname.match(STEP);
    if (m) {
      u.pathname = m[1];
      u.search = "";
    }
    u.pathname = u.pathname.replace(/\/+$/, "") || "/";
    return u.toString();
  }

  const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", "#39": "'", nbsp: " " };

  const decode = (s) => s.replace(/&(#\d+|#x[0-9a-f]+|\w+);/gi, (m, e) => {
    if (e[0] === "#") return String.fromCodePoint(e[1].toLowerCase() === "x" ? parseInt(e.slice(2), 16) : +e.slice(1));
    return ENTITIES[e.toLowerCase()] ?? m;
  });

  function htmlToText(html) {
    // Decode first: many job sites HTML-escape the description inside JSON-LD (&lt;p&gt;).
    return decode(decode(String(html || ""))
      .replace(/<(br|\/p|\/li|\/div|\/h[1-6]|\/tr)\b[^>]*>/gi, "\n")
      .replace(/<li\b[^>]*>/gi, "\n- ")
      .replace(/<[^>]+>/g, ""))
      .split("\n").map((l) => l.replace(/\s+/g, " ").trim()).filter(Boolean).join("\n");
  }

  function isJobPosting(node) {
    const t = node && node["@type"];
    return t === "JobPosting" || (Array.isArray(t) && t.includes("JobPosting"));
  }

  /** {role, company, jd} from the page's JSON-LD script texts, or null. */
  function parseJobPosting(jsonLdTexts) {
    for (const text of jsonLdTexts) {
      let data;
      try { data = JSON.parse(text); } catch { continue; }
      const nodes = [].concat(data).flatMap((n) => (n && n["@graph"] ? n["@graph"] : [n]));
      const job = nodes.find(isJobPosting);
      if (!job) continue;
      const org = job.hiringOrganization;
      const jd = htmlToText(job.description);
      if (!jd) continue;
      return {
        // Titles are HTML-escaped too on some sites (Workday: "AI &amp; Agentic").
        role: htmlToText(job.title).replace(/\n/g, " "),
        company: htmlToText((org && (org.name || (typeof org === "string" ? org : ""))) || "").replace(/\n/g, " "),
        jd,
      };
    }
    return null;
  }

  const CONFIRM_URL = /\/(confirmation|thanks|thank-you|submitted|success)(\/|$|\?)/i;
  const CONFIRM_TEXT = /(thank you for applying|thanks for applying|application (has been |was )?(submitted|received)|we('ve| have) received your application|your application has been sent)/i;

  /** Does this page look like an application-submitted confirmation? */
  function isConfirmation(href, pageText) {
    return CONFIRM_URL.test(new URL(href).pathname + "/") || CONFIRM_TEXT.test(pageText || "");
  }

  /** Badge lines from a GET /api/check response: prior contact + where this posting stands. */
  function badgeLines(check) {
    const lines = [];
    const lead = check.lead;
    if (lead && lead.staged_to && lead.staged_to.length) lines.push(`Reach-out staged to ${lead.staged_to[0]}`);
    else if (lead && lead.applied_resume) lines.push(`Applied with "${lead.applied_resume}"`);
    else if (lead && lead.jd) lines.push("Captured");
    for (const c of check.company_contacts || []) {
      lines.push(`Already contacted ${c.recipient} (${c.status}${c.replied_at ? ", replied" : ""})`);
    }
    if (!(check.company_contacts || []).length) lines.push("No prior contact at this company");
    return lines;
  }

  /** One sentence on what the reach-out is waiting for. */
  function emailStatus(check) {
    const lead = check.lead;
    const email = check.email;
    if (!lead) return "";
    if (lead.staged_to && lead.staged_to.length) return `Reach-out staged to ${lead.staged_to[0]}. It sends on the next runner.`;
    if (!lead.campaign) return "Pick a campaign so slap can set up the reach-out.";
    if (!email) return "";
    if (email.error) return email.error;
    const missing = email.missing || [];
    if (missing.includes("contact email")) return "Add the contact's email to send a reach-out.";
    if (missing.length) return "Waiting for the drop: in Claude Code, ask it to write the drop for this application.";
    return lead.applied_resume ? "Email ready." : "Email ready. It stages when you click Applied.";
  }

  // --- form filling ---------------------------------------------------------

  // Never filled, never sent anywhere: credentials, payment, government ID.
  const SENSITIVE = /password|passcode|\bssn\b|social security|passport|driver'?s? licen[cs]e|national id|tax id|card number|\bcvv\b|\biban\b|routing number|bank account|date of birth|\bdob\b/i;
  // Voluntary self-identification: yours to answer, never guessed or sent to Claude.
  const EEO = /gender|\bsex\b|\brace\b|ethnic|veteran|disabilit|sexual orientation|lgbt|pronoun|transgender|hispanic|latino|religio|\bage\b/i;
  // A choice question offering "decline to identify" is self-ID whatever its label says.
  const EEO_OPTION = /decline to (self[- ])?identify|prefer not to (say|answer|disclose)|don'?t wish to (answer|identify)/i;

  // Label -> profile field. Order matters: specific before general.
  const PROFILE_RULES = [
    ["sponsorship", /sponsor/i],
    ["work_authorization", /authori[sz]ed to work|legally (eligible|authori[sz]ed|permitted)|work authori[sz]ation|right to work/i],
    ["current_company", /current (company|employer)|present employer|most recent (company|employer)|^company( name)?\*?$/i],
    ["current_title", /current (job )?(title|role|position)|^job title\*?$|^title\*?$/i],
    ["linkedin", /linkedin/i],
    ["github", /github/i],
    ["website", /website|portfolio|personal (site|url|page)/i],
    ["email", /e-?mail/i],
    ["phone", /phone|mobile|\btel\b/i],
    ["first_name", /first name|given name|^first$/i],
    ["last_name", /last name|surname|family name|^last$/i],
    ["full_name", /^(full |legal )?name\*?$|your name|full name/i],
    ["location", /^location|current location|city|where are you (based|located)/i],
  ];

  function profileValue(label, fields) {
    for (const [key, re] of PROFILE_RULES) {
      if (!re.test(label)) continue;
      if (key === "full_name") return fields.full_name || [fields.first_name, fields.last_name].filter(Boolean).join(" ") || null;
      return fields[key] || null;
    }
    return null;
  }

  /** The option a select/radio should take for `value`, or null if none fits. */
  function matchOption(options, value) {
    const v = String(value).trim().toLowerCase();
    const opts = options.map((o) => [o, o.trim().toLowerCase()]);
    // Exact, else an option that starts with the answer ("Yes" -> "Yes, I am
    // authorized"). Never the reverse: "None" must not pick "No".
    const hit = opts.find(([, o]) => o === v) || (v && opts.find(([, o]) => o.startsWith(v)));
    return hit ? hit[0] : null;
  }

  /**
   * Proposed value per question: [{label, value, source}], where source is
   * "claude" | "bank" | "profile" | "sensitive" | "eeo" | null (unanswered).
   * Precedence: exclusions, then Claude's answers for THIS posting, then the
   * answer bank, then profile fields. Choice questions only take a value that
   * matches one of their options.
   */
  function planFill(questions, profile, leadAnswers) {
    const bankRules = (profile.answers || []).map((a, i) => {
      try {
        return { re: new RegExp(a.match, "i"), answer: a.answer };
      } catch (e) {
        throw new Error(`config.yaml profile.answers[${i}].match ${JSON.stringify(a.match)} is not a valid JavaScript regex: ${e.message}`);
      }
    });
    return questions.map((q) => {
      const label = q.label;
      if (SENSITIVE.test(label)) return { label, value: null, source: "sensitive" };
      if (EEO.test(label) || (q.options || []).some((o) => EEO_OPTION.test(o))) {
        return { label, value: null, source: "eeo" };
      }
      const bank = bankRules.find((r) => r.re.test(label));
      const candidates = [
        [(leadAnswers || {})[label], "claude"],
        [bank && bank.answer, "bank"],
        [profileValue(label, profile.fields || {}), "profile"],
      ];
      for (const [raw, source] of candidates) {
        if (raw == null || raw === "") continue;
        const value = q.options && q.options.length ? matchOption(q.options, raw) : raw;
        if (value != null) return { label, value, source };
      }
      return { label, value: null, source: null };
    });
  }

  /** Short form of a field label for display. The full text stays the
   *  question's key (matching, Claude). Drops a trailing required "*", and
   *  cuts long help-text labels to the first question or sentence. */
  function displayLabel(text) {
    const t = String(text || "").replace(/\s*\*\s*$/, "").trim();
    if (t.length <= 90) return t;
    const q = t.indexOf("?");
    if (q > 0 && q < 140) return t.slice(0, q + 1);
    const dot = t.search(/[.:](\s|$)/);
    return (dot > 0 && dot < 90 ? t.slice(0, dot + 1) : t.slice(0, 87).trimEnd() + "…");
  }

  const api = { canonicalJobUrl, displayLabel, emailStatus, isStepPage, htmlToText, parseJobPosting, isConfirmation, badgeLines,
                matchOption, planFill };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.slapLib = api;
})(globalThis);
