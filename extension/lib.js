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
    else if (lead) lines.push("Captured, not matched yet: run slap_leads in Claude Code");
    for (const c of check.company_contacts || []) {
      lines.push(`Already contacted ${c.recipient} (${c.status}${c.replied_at ? ", replied" : ""})`);
    }
    if (!lead && !(check.company_contacts || []).length) lines.push("No prior contact found");
    return lines;
  }

  const api = { canonicalJobUrl, isStepPage, htmlToText, parseJobPosting, isConfirmation, badgeLines };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.slapLib = api;
})(globalThis);
