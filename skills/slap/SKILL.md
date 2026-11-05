---
name: slap
description: Work on ONE job application through the slap MCP server. Write the reach-out "drop" (the campaign's field values) for the application the owner is filling in, or answer that application's open form questions. Use when the owner says things like "write the drop", "draft the email for this application", "answer this application's questions", or mentions slap and a job they're applying to.
---

# slap: one application at a time

slap's browser extension has already captured the posting (JD, role, company, URL), the campaign, the résumé and the contact. Your part is narrow: write the drop, and answer form questions the extension couldn't fill. You never send anything. The reach-out stages only after the owner clicks **Applied** in the extension.

## Scope rules

- Start every task with `slap_application`. With no `lead_id`, it returns the application the owner touched most recently in the browser.
- First line of your reply: `Working on <company> · <role>`, so the owner can correct you if that's the wrong one.
- Stay on that one application. Don't list, compare or bring up other applications unless the owner asks. If they name a different one, call `slap_application` with its `lead_id`.
- If it has no campaign yet, say so and stop. The owner picks one in the extension panel.

## Write the drop

1. From `slap_application`, read `jd`, `drop_fields` (each with `key`, `label`, `hint`, `optional`), `template`, and the current `drop` and `email_preview`.
2. Choose the 2–4 most relevant entries from the `stars` index (by title and tags) and fetch them with `slap_stars`. Those entries are the only source of claims.
3. Write a value for every required drop field, following its `hint`. Leave an optional field empty when nothing real fits; its line is dropped from the email.
4. Show the owner the values in chat, then call `slap_save_drop`. Its permission prompt is the owner's confirm. The result has the rendered `email` and an `outreach` status:
   - `not_applied`: saved. Tell the owner it stages when they click Applied.
   - `staged`: the owner had already applied, so the reach-out is now queued. Say who it goes to.
   - `incomplete`: say exactly what's missing. A contact email is set in the extension panel.
   - `needs_confirm`: show the warnings (e.g. already contacted). Re-call with `acknowledge_warnings: true` only if the owner says yes.
5. To revise, save again. The latest drop wins until the reach-out is staged.

## Answer form questions

1. Take `open_questions` from `slap_application`.
2. Answer each one from the JD and `slap_stars` entries only. For a select or radio question, answer with one of its `options`, verbatim.
3. Never answer self-identification (gender, race, veteran, disability), salary, or legal or ID questions. Leave those to the owner.
4. Save with `slap_answer_questions`, keyed by the exact labels. Then tell the owner to click **Re-check answers** in the panel.

## Writing rules

- Claims come only from `slap_stars` entries. Never invent a metric, employer, tool or credit.
- Plain and specific. No em dashes, no "not just X, but Y", and no three-part list rhythms.
- Keep each field to the length its hint asks for.
- *Owner's voice and personal rules: pending. They'll be added here once the reference files (star.json, story.md, …) are final.*
