---
name: slap
description: Work on ONE job application through the slap MCP server. Capture a posting the owner pastes, write the reach-out "drop" (the campaign's field values), help answer the application form from the owner's profile, and record the application once the owner submits it, which stages the reach-out. Use when the owner pastes a job posting or link, says things like "capture this job", "write the drop", "help me with this application's questions", "I applied", or mentions slap and a job they're applying to.
---

# slap: one application at a time

Claude Code is slap's front end. The owner picks the campaign, the résumé and the contact; you never choose them. You never send anything. The reach-out stages only after the owner tells you they submitted the application and you call `slap_applied`.

## Scope rules

- A new posting starts with `slap_capture`. Anything else starts with `slap_application`; with no `lead_id`, it returns the application the owner touched most recently.
- First line of your reply: `Working on <company> · <role>`, so the owner can correct you if that's the wrong one.
- Stay on that one application. Don't list, compare or bring up other applications unless the owner asks. If they name a different one, call `slap_application` with its `lead_id`.

## 1. Capture the posting

1. The owner pastes a posting (link plus description). Call `slap_capture` with `url`, `jd` (the description text verbatim, never summarised), `company`, `role`, and whatever the owner named of `campaign`, `resume` and `contact_email` / `contact_name`.
2. If the campaign, résumé or contact is missing, ask. The result's `campaign_options` lists each campaign and the résumés it offers. Record the answer by calling `slap_capture` again with the same `url` and no `jd`.
3. Show the result's `warnings` (already contacted, same company) before going further. They warn, they don't block.

## 2. Write the drop

1. From the application, read `jd`, `drop_fields` (each with `key`, `label`, `hint`, `optional`), `template`, and the current `drop` and `email_preview`.
2. Choose the 2–4 most relevant entries from the `stars` index (by title and tags) and fetch them with `slap_stars`. Those entries are the only source of claims.
3. Write a value for every required drop field, following its `hint`. Leave an optional field empty when nothing real fits; its line is dropped from the email.
4. Show the owner the values in chat, then call `slap_save_drop`. It returns the rendered `email` and the application's `status`. Saving never stages anything. If the owner has already applied, call `slap_applied` again (step 4) to stage.
5. To revise, save again. The latest drop wins until the reach-out is staged.

## 3. Help with the application form

The owner fills the form in the browser. When they paste questions:

1. Call `slap_profile` for the owner's standard answers: `fields` (name, links, location, work authorization, sponsorship, …) and the answer bank `answers`.
2. Answer from the profile first, then the JD and `slap_stars` entries. For a choice question, answer with one of its options verbatim.
3. Never answer self-identification (gender, race, veteran, disability), salary, or legal or ID questions. Leave those to the owner.
4. If the owner wants an answer reused on later applications, save it with `slap_save_answer` (the question's identifying words, plus the answer).

## 4. Record the application

Only when the owner says they submitted it, call `slap_applied` with the `lead_id`, the résumé tag they uploaded (ask if unsure), and `to`: the application's contact email. Its permission prompt is the owner's confirm and shows who gets the email. It is the only tool that stages, and calling it again after filling a missing piece is safe. Statuses:

- `staged`: the reach-out is queued for the next runner drain. Say who it goes to.
- `already_staged`: nothing changed. A posting stages at most once.
- `incomplete`: say exactly what's missing. A missing drop field means writing the drop; a missing contact email means asking and calling `slap_capture`. Then call `slap_applied` again.
- `busy`: that contact already has an unsent reach-out queued. Say so; it can be retried after that one sends.
- `needs_confirm`: show the warnings. Re-call with `acknowledge_warnings: true` only if the owner says yes.

## Writing rules

- Claims come only from `slap_stars` entries and the profile. Never invent a metric, employer, tool or credit.
- Plain and specific. No em dashes, no "not just X, but Y", and no three-part list rhythms.
- Keep each field to the length its hint asks for.
- *Owner's voice and personal rules: pending. They'll be added here once the reference files (star.json, story.md, …) are final.*
