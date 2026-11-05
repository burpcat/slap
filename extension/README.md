# slap Chrome extension

The browser half of slap.

1. **On a posting:** pick the campaign and résumé, enter the contact's email and name, and click **Capture**. The JD, role, company and URL go to slap with those details.
2. **On the application form:**
   - The panel prepares answers from the `profile:` section of `config.yaml` by itself, and shows each value with its source. Anything sensitive or self-identifying is left for you.
   - **Fill checked** fills the ticked rows, and **Attach résumé** uploads the selected PDF.
   - Leftover questions go to Claude with **Ask Claude**, or automatically after filling if you tick *Auto-send unanswered questions to Claude*. In Claude Code, ask it to answer this application's questions, then click **Re-check answers**.
3. **Drop:** in Claude Code, ask it to write the drop for this application (the `slap` skill). The panel previews the email.
4. **Applied:** after you submit, click **Applied**. That records the résumé you used and stages the reach-out once it has a drop and a contact. It sends on the next runner.

It never clicks Submit, and never fills or records anything without your click. It loads by itself on Greenhouse, Lever, Ashby and Workday. Workday works field by field: its own dropdowns and repeatable sections (work history, education) are still yours to fill. On any other page, or after closing the panel, click the toolbar icon.

## Install (Chrome, Edge or Brave)

1. Keep slap's API running: `python slap.py dashboard`, or install the LaunchAgent from `python slap.py plist --job dashboard` (see LAUNCHD.md).
2. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and pick this `extension/` folder.
3. Copy the extension's 32-letter ID from that page into `config.yaml`:
   ```yaml
   extension_id: abcdefghijklmnopabcdefghijklmnop
   ```
   Then restart the dashboard. Until you do, slap refuses the extension's requests (403), and the panel tells you so.

The ID of an unpacked extension stays the same as long as you load it from the same folder path.

## Develop

- `lib.js` holds the pure logic: job parsing, URL canonicalising, confirmation detection, panel status text, and the form-fill plan.
- Tests live in `tests/extension/`, outside this folder, so Chrome never loads test dependencies. Run `npm --prefix tests/extension install && node --test --test-force-exit tests/extension/*.test.js`. `content.test.js` drives the whole panel in jsdom against a mock application form.
- `background.js` is the only code that talks to `http://127.0.0.1:5050`. `content.js` is the page panel.
