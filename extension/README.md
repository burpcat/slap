# slap Chrome extension

The browser half of slap. On a job posting it:

- shows a small panel: company · role, whether you've already contacted anyone there, and the résumé Claude matched;
- **Send to slap** captures the posting (JD, company, role, URL) as a lead for Claude Code (`slap_leads` MCP tool);
- after you submit an application, **Mark applied** records which résumé you uploaded. That releases any email Claude staged with `hold_until_applied`.

It never clicks Submit and never records an application without your click. It loads automatically on Greenhouse, Lever, Ashby and Workday. On any other page, click the toolbar icon.

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

- `lib.js` holds the pure logic: job parsing, URL canonicalising, confirmation detection, badge text, and the form-fill plan.
- Tests live in `tests/extension/`, outside this folder, so Chrome never loads test dependencies. Run `npm --prefix tests/extension install && node --test tests/extension/*.test.js`. `content.test.js` drives the whole panel in jsdom against a mock application form.
- `background.js` is the only code that talks to `http://127.0.0.1:5050`. `content.js` is the page panel.
