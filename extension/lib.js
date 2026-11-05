// Pure, DOM-free helpers shared by content.js and the node tests (lib.test.js).
(function (root) {
  "use strict";

  // Path tails that are steps of the same posting, not a different job.
  const STEP_TAIL = /\/(apply|application|confirmation|thanks|thank-you|submitted)(\/.*)?$/i;

  /** Is this an apply/confirmation step of a posting rather than the posting itself? */
  function isStepPage(href) {
    return STEP_TAIL.test(new URL(href).pathname);
  }

  /** One URL per posting, so the apply/confirmation pages find the captured lead. */
  function canonicalJobUrl(href) {
    const u = new URL(href);
    u.hash = "";
    u.pathname = u.pathname.replace(STEP_TAIL, "").replace(/\/+$/, "") || "/";
    return u.toString();
  }

  const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", "#39": "'", nbsp: " " };

  function htmlToText(html) {
    return String(html || "")
      .replace(/<(br|\/p|\/li|\/div|\/h[1-6]|\/tr)\b[^>]*>/gi, "\n")
      .replace(/<li\b[^>]*>/gi, "\n- ")
      .replace(/<[^>]+>/g, "")
      .replace(/&(#\d+|#x[0-9a-f]+|\w+);/gi, (m, e) => {
        if (e[0] === "#") return String.fromCodePoint(e[1].toLowerCase() === "x" ? parseInt(e.slice(2), 16) : +e.slice(1));
        return ENTITIES[e.toLowerCase()] ?? m;
      })
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
        role: String(job.title || "").trim(),
        company: String((org && (org.name || (typeof org === "string" ? org : ""))) || "").trim(),
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

  /** Badge lines from a GET /api/check response. */
  function badgeLines(check) {
    const lines = [];
    const lead = check.lead;
    if (lead && lead.applied_resume) lines.push(`Applied with "${lead.applied_resume}"`);
    else if (lead && lead.resume) lines.push(`Matched résumé: "${lead.resume}" (${lead.campaign})`);
    else if (lead) {
      lines.push("Captured, not matched yet: run slap_leads in Claude Code");
      if (check.suggested_resume) lines.push(`Likely résumé: "${check.suggested_resume.tag}" (local guess)`);
    }
    for (const c of check.company_contacts || []) {
      lines.push(`Already contacted ${c.recipient} (${c.status}${c.replied_at ? ", replied" : ""})`);
    }
    if (!lead && !(check.company_contacts || []).length) lines.push("No prior contact found");
    return lines;
  }

  // --- form filling ---------------------------------------------------------

  // Never filled, never sent anywhere: credentials, payment, government ID.
  const SENSITIVE = /password|passcode|\bssn\b|social security|passport|driver'?s? licen[cs]e|national id|tax id|card number|\bcvv\b|\biban\b|routing number|bank account|date of birth|\bdob\b/i;
  // Voluntary self-identification: yours to answer, never guessed or sent to Claude.
  const EEO = /gender|\brace\b|ethnic|veteran|disabilit|sexual orientation|pronoun|transgender|hispanic|latino/i;

  // Label -> profile field. Order matters: specific before general.
  const PROFILE_RULES = [
    ["sponsorship", /sponsor/i],
    ["work_authorization", /authori[sz]ed to work|legally (eligible|authori[sz]ed|permitted)|work authori[sz]ation|right to work/i],
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
    const hit = opts.find(([, o]) => o === v) || opts.find(([, o]) => o && (o.startsWith(v) || v.startsWith(o)));
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
    return questions.map((q) => {
      const label = q.label;
      if (SENSITIVE.test(label)) return { label, value: null, source: "sensitive" };
      if (EEO.test(label)) return { label, value: null, source: "eeo" };
      const bank = (profile.answers || []).find((a) => new RegExp(a.match, "i").test(label));
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

  const api = { canonicalJobUrl, isStepPage, htmlToText, parseJobPosting, isConfirmation, badgeLines,
                matchOption, planFill };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.slapLib = api;
})(globalThis);
