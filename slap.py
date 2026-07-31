#!/usr/bin/env python3
"""slap.py - personal cold job-outreach CLI over the GMass API.

See SLAP_BUILD_PROMPT.md for the full spec and CONTROL_SHEET.md for the
current build state / package layout.
"""
import argparse
import difflib
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from slap import display
from slap.cleanup import DEFAULT_MIN_DAYS_IDLE, delete_eligible, find_cleanup_candidates
from slap.config import (
    ConfigError, discover_campaigns, load_campaign, load_global_config, parse_initial_txt_text,
)
from slap.latex import recipient_workdir, run_latex_loop
from slap.prompts import PASTE_TERMINATOR, read_paste
from slap.queue import AmbiguousArchiveChoice, QueueError, resend_bounced, stage_recipient
from slap.templates import fill_template, merge_config_values, parse_drop
from slap import (
    archive, dashboard, doctor, domains, followups, gmass, gmass_cache, init, launchd, onboard,
    reload, runner, tracking,
)


def cmd_list(args):
    try:
        global_config = load_global_config()
    except ConfigError as e:
        display.fail(f"slap: {e}")
        sys.exit(1)

    names = discover_campaigns()
    if not names:
        print("No campaigns found under campaigns/.")
        return

    for name in names:
        try:
            campaign = load_campaign(name, global_config)
        except ConfigError as e:
            display.error(f"{name}: ERROR — {e}")
            continue
        latex_state = "latex on" if campaign.latex_enabled else "latex off"
        print(f"{name}  persona={campaign.persona}  {latex_state}")


def _run_doctor_or_exit(global_config, campaign=None):
    """Auto-preflight before any send (§11) — a subset of `doctor`'s own
    checks: the global battery always, plus this one campaign's checks when
    a target campaign is known. Runs BEFORE domains.load_consumer_domains()
    so an owner who's never run `doctor` and is missing consumer_domains.txt
    gets it auto-seeded here rather than hitting that call's fail-loud path."""
    results = doctor.run_global_checks(global_config)
    if campaign is not None:
        results += doctor.run_campaign_checks(campaign)
    failures = [r for r in results if not r.ok]
    if failures:
        lines = "\n".join(f"  - {r.name}: {r.detail}" for r in failures)
        display.fail(f"slap: doctor preflight failed — run `slap.py doctor` for details:\n{lines}")
        sys.exit(1)


def cmd_send(args):
    # `slap.py send custom` is a distinct one-off mode, not a real campaign
    # folder — "custom" is a reserved campaign token (a campaigns/custom/ folder
    # would be shadowed by this branch; documented in USAGE).
    if args.campaign == "custom":
        return cmd_send_custom(args)
    try:
        global_config = load_global_config()
        campaign = load_campaign(args.campaign, global_config)
    except ConfigError as e:
        display.fail(f"slap: {e}")
        sys.exit(1)

    _run_doctor_or_exit(global_config, campaign)

    try:
        consumer_domains = domains.load_consumer_domains(Path(global_config.consumer_domains_file))
    except domains.DomainsError as e:
        display.fail(f"slap: {e}")
        sys.exit(1)

    conn = tracking.connect()

    archive_dir = archive.archive_dir_from_env()
    if archive_dir is None:
        display.plain(f"Résumé archive is off ({archive.ENV_VAR} not set) — see .env.example to enable.")

    while True:
        drop_text = read_paste(f"\nPaste the drop for campaign '{campaign.name}'")
        values = parse_drop(drop_text, campaign.fields)

        recipient = values.get("email", "").strip()
        if not recipient:
            display.error("No 'Email' value found in the drop — skipping this recipient.")
        else:
            _prep_one_recipient(conn, campaign, consumer_domains, values, recipient, archive_dir,
                                signature=global_config.signature)

        if input("\nAdd another? [Y/n]: ").strip().lower() == "n":
            break

    if args.now:
        print("\n--now: draining the queue immediately...")
        result = runner.drain(conn, global_config, os.environ.get(global_config.api_key_env, ""))
        _print_drain_result(result)


CUSTOM_CAMPAIGN = "__custom__"  # reserved pseudo-campaign label for `send custom`


def _open_in_editor(editor_cmd: str, path: Path) -> None:
    """Open `path` in the configured editor and BLOCK until it returns. The
    editor command is a full string (`code --wait`, `vim`, ...) so GUI editors
    can carry their own wait-flag — see config.editor's own docstring for why a
    bare `code` would return immediately and read back stale content."""
    parts = editor_cmd.split()
    try:
        subprocess.run([*parts, str(path)], check=False)
    except FileNotFoundError:
        raise ConfigError(
            f"editor command {parts[0]!r} not found on PATH — set a valid `editor:` in config.yaml"
        )


def _ask_positive_int(prompt: str, *, read_line=input) -> int:
    while True:
        raw = read_line(prompt).strip()
        try:
            n = int(raw)
        except ValueError:
            display.warn("  Enter a whole number >= 1.")
            continue
        if n >= 1:
            return n
        display.warn("  Enter a whole number >= 1.")


def _choose_custom_attachment(workdir: Path, recipient: str, *, read_line=input):
    """Resolve the attachment for a custom send. Returns
    (attachment_path|None, attachment_name|None, latex_enabled). Four modes
    (per the owner's spec): pick a PDF already in this send's folder, paste an
    absolute path, author LaTeX now (compile + the >1-page hard gate), or none."""
    while True:
        display.plain("\nAttachment:")
        display.plain(f"  1. Pick a PDF already placed in this send's folder ({workdir})")
        display.plain("  2. Paste an absolute path to a PDF")
        display.plain("  3. Write a LaTeX résumé now (compile + >1-page gate)")
        display.plain("  4. No attachment")
        choice = read_line("Choose [1-4]: ").strip()
        if choice == "1":
            pdfs = sorted(workdir.glob("*.pdf"))
            if not pdfs:
                display.warn(f"  No PDFs found in {workdir} — place one there first, or pick another option.")
                continue
            for i, p in enumerate(pdfs, start=1):
                display.plain(f"  {i}. {p.name}")
            sel = read_line(f"Pick [1-{len(pdfs)}]: ").strip()
            try:
                picked = pdfs[int(sel) - 1]
            except (ValueError, IndexError):
                display.warn("  Not understood.")
                continue
            return picked, picked.name, False
        if choice == "2":
            p = Path(read_line("Absolute path to a PDF: ").strip()).expanduser()
            if not p.is_file():
                display.warn(f"  {p} is not a file.")
                continue
            if p.suffix.lower() != ".pdf":
                display.warn(f"  {p} is not a .pdf.")
                continue
            return p, p.name, False
        if choice == "3":
            name = read_line("Attachment filename the recipient sees [Resume.pdf]: ").strip() or "Resume.pdf"
            tex_source = read_paste(f"\nPaste the LaTeX résumé source for {recipient}")
            staged = run_latex_loop(workdir, tex_source, name)
            if staged is None:
                display.warn("  LaTeX aborted — choose an attachment option again.")
                continue
            return staged.path, name, True
        if choice == "4":
            return None, None, False
        display.warn("  Enter 1, 2, 3, or 4.")


def cmd_send_custom(args):
    """`slap.py send custom` — author a one-off message (+ optional custom-cadence
    follow-ups) in the configured editor, choose an attachment, and stage it via
    the SAME queue/runner machinery as a normal send (campaign='__custom__',
    persona='custom', an explicit per-recipient cadence). No campaign.yaml is
    ever created; auto-discovery never sees this."""
    try:
        global_config = load_global_config()
    except ConfigError as e:
        display.fail(f"slap: {e}")
        sys.exit(1)

    # Fail loud NOW if the editor isn't usable (this command's one hard prereq).
    editor_check = doctor.check_editor(global_config)
    if not editor_check.ok:
        display.fail(f"slap: {editor_check.detail} — set a valid `editor:` in config.yaml")
        sys.exit(1)

    try:
        consumer_domains = domains.load_consumer_domains(Path(global_config.consumer_domains_file))
    except domains.DomainsError as e:
        display.fail(f"slap: {e}")
        sys.exit(1)

    conn = tracking.connect()
    recipient = input("\nRecipient email: ").strip()
    if not recipient:
        display.error("No recipient email — aborting.")
        return 1

    workdir = recipient_workdir(CUSTOM_CAMPAIGN, recipient)

    # Initial email: Subject: line + blank line + body, authored in the editor
    # (same shape every initial.txt uses, validated by the same parser).
    initial_path = workdir / "initial.txt"
    if not initial_path.exists():
        initial_path.write_text("Subject: \n\n", encoding="utf-8")
    print("\nOpening your editor for the initial email (first line 'Subject: ...', blank line, then body)...")
    _open_in_editor(global_config.editor, initial_path)
    try:
        subject, body = parse_initial_txt_text(initial_path.read_text(encoding="utf-8"), ctx=str(initial_path))
    except ConfigError as e:
        display.fail(f"slap: {e}")
        return 1

    # Custom cadence: each follow-up asks its own day-gap, then opens the editor
    # for that stage's body. This per-stage day-gap list IS the cadence staged
    # for this recipient (recipients.cadence), no persona involved.
    cadence, stage_bodies = [], []
    while True:
        if input(f"\nAdd a follow-up (stage {len(cadence) + 1})? [y/N]: ").strip().lower() != "y":
            break
        gap = _ask_positive_int(f"Days after the previous message before stage {len(cadence) + 1} fires: ")
        stage_path = workdir / f"stage{len(cadence) + 1}.txt"
        if not stage_path.exists():
            stage_path.write_text("", encoding="utf-8")
        print(f"Opening your editor for stage {len(cadence) + 1}'s body...")
        _open_in_editor(global_config.editor, stage_path)
        cadence.append(gap)
        stage_bodies.append(stage_path.read_text(encoding="utf-8"))

    attachment_path, attachment_name, latex_enabled = _choose_custom_attachment(workdir, recipient)

    # Dedup awareness — warn, never block (same as a normal send).
    dedup = domains.check_recipient(conn, recipient, consumer_domains)
    if dedup.hard_warning:
        w = dedup.hard_warning
        display.error(f"\n⚠ HARD WARN: {recipient} already contacted — campaign={w.campaign} status={w.status}")
    if dedup.soft_warning_contacts:
        display.warn(f"\n⚠ SOFT WARN: {len(dedup.soft_warning_contacts)} other contact(s) on domain "
                     f"{dedup.soft_warning_domain}")
    if (dedup.hard_warning or dedup.soft_warning_contacts) and \
            input(display.styled_prompt("Proceed anyway? [y/N]: ", style=display.YELLOW)).strip().lower() != "y":
        print("Skipped.")
        return

    display.preview_panel(recipient, subject, body)
    print(f"Attachment: {attachment_name if attachment_name else '(none)'}")
    print(f"Cadence (custom): {cadence}" if cadence else "Cadence: initial send only (no follow-ups)")
    # Defaults to YES, same as the normal `send` flow: preview + dedup are
    # already behind us, so a bare Enter stages; only an explicit n/no skips.
    if input("\nStage this custom send? [Y/n]: ").strip().lower() in ("n", "no"):
        print("Skipped.")
        return

    stage_recipient(
        conn, campaign=CUSTOM_CAMPAIGN, recipient=recipient, persona="custom",
        cadence=cadence, subject=subject, body=body, stage_bodies=stage_bodies,
        attachment_path=attachment_path, attachment_name=attachment_name, latex_enabled=latex_enabled,
        archive_dir=archive.archive_dir_from_env(),
    )
    display.success(f"Staged {recipient} (custom send).")

    if args.now:
        print("\n--now: draining the queue immediately...")
        result = runner.drain(conn, global_config, os.environ.get(global_config.api_key_env, ""))
        _print_drain_result(result)


def _warn_empty_fields(campaign, values) -> None:
    """Pre-preview validation warning (display-only, never blocks): a drop
    that leaves a declared field empty is often a paste mistake worth
    flagging, but some fields (req_id) are legitimately blank often — so
    this only warns, the send still proceeds normally on confirm. 'Empty'
    matches fill_template's own definition (exact '', not stripped) so this
    warning and the optional-field line-drop behavior never disagree about
    what counts as empty. Nothing here touches subject/body/values."""
    empty_keys = [f.key for f in campaign.fields if values.get(f.key, "") == ""]
    if empty_keys:
        display.warn(f"⚠ empty fields: {', '.join(empty_keys)}")


def _offer_resume_reuse(matches: list, *, read_line=input):
    """Numbered choice of previously-archived résumés to reuse instead of
    the campaign's default static attachment — offered only when the domain
    soft-warn fires and matches exist for this company (see caller). Returns
    the chosen archive entry Path, or None if declined. '0'/empty input both
    mean 'no reuse' and are the easy/default answer — this is an offer, not
    a nudge toward reuse."""
    display.plain(f"\n{len(matches)} previous résumé(s) found in the archive for this company:")
    for i, m in enumerate(matches, start=1):
        display.plain(f"  {i}. {m.name}")
    display.plain("  0. Use this campaign's default resume")
    while True:
        raw = read_line(display.styled_prompt(
            "Reuse one of these instead? [0]: ", style=display.YELLOW
        )).strip()
        if raw in ("", "0"):
            return None
        try:
            choice = int(raw)
        except ValueError:
            display.warn(f"  Not understood — enter a number 0-{len(matches)}.")
            continue
        if 1 <= choice <= len(matches):
            return matches[choice - 1]
        display.warn(f"  Not understood — enter a number 0-{len(matches)}.")


def _ask_followup_count(campaign, *, read_line=input) -> int:
    """Per-recipient follow-up override (post-launch): lets the owner choose,
    for THIS recipient only, how many of the persona's configured follow-up
    stages actually go out — 0 (initial send only) up to the persona's full
    cadence length. Always shown (never opt-in — an explicit owner decision,
    per the same "warn/ask, don't hide" convention every other prompt in this
    flow follows), defaulting to the persona's full cadence on a bare Enter
    so the common case (no override) costs nothing but reading one line.
    Returns an int in [0, len(campaign.cadence)] — the caller truncates
    `campaign.cadence`/`stage_bodies` to a PREFIX of this length (a persona's
    cadence is a fixed, ordered day-offset sequence; there's no notion of
    skipping stage 2 but keeping stage 3)."""
    max_n = len(campaign.cadence)
    while True:
        raw = read_line(
            f"\nFollow-ups for this recipient? [0-{max_n}, default {max_n}] "
            f"(persona={campaign.persona} cadence {campaign.cadence}): "
        ).strip()
        if raw == "":
            return max_n
        try:
            n = int(raw)
        except ValueError:
            display.warn(f"  {raw!r} is not a whole number — enter 0-{max_n}.")
            continue
        if 0 <= n <= max_n:
            return n
        display.warn(f"  Must be between 0 and {max_n}.")


def _prep_one_recipient(conn, campaign, consumer_domains, values, recipient, archive_dir, *,
                         signature: str, read_line=input):
    if campaign.latex_enabled:
        tex_source = read_paste(f"\nPaste the LaTeX résumé source for {recipient}")
        workdir = recipient_workdir(campaign.name, recipient)
        staged = run_latex_loop(workdir, tex_source, campaign.attachment_name)
        if staged is None:
            print("Aborted — nothing staged for this recipient.")
            return
        attachment_path = staged.path
    else:
        attachment_path = campaign.path / campaign.attachment_file
        placeholder_check = doctor.check_placeholder_resume(campaign)
        if not placeholder_check.ok:
            display.warn(f"⚠ {placeholder_check.detail}")

    dedup = domains.check_recipient(conn, recipient, consumer_domains)
    if dedup.hard_warning:
        w = dedup.hard_warning
        replied = "yes" if w.replied_at else "no"
        display.error(f"\n⚠ HARD WARN: {recipient} already contacted — campaign={w.campaign} "
                      f"status={w.status} first_sent={w.first_sent_at} replied={replied}")
    if dedup.soft_warning_contacts:
        display.warn(f"\n⚠ SOFT WARN: {len(dedup.soft_warning_contacts)} other contact(s) already on "
                     f"domain {dedup.soft_warning_domain}:")
        for c in dedup.soft_warning_contacts:
            display.warn(f"    {c.recipient}  campaign={c.campaign}  status={c.status}")
    if (dedup.hard_warning or dedup.soft_warning_contacts) and \
            read_line(display.styled_prompt("Proceed anyway? [y/N]: ", style=display.YELLOW)).strip().lower() != "y":
        print("Skipped.")
        return

    # Résumé reuse (v1: latex-off campaigns only — see CONTROL_SHEET.md).
    # Only offered on the domain SOFT warn (a different person at the same
    # company was already contacted) — never the hard warn (this exact
    # recipient already contacted), and only when the archive actually has
    # a matching entry for this company; otherwise this is a no-op and
    # behavior is byte-for-byte identical to before this feature existed.
    reused_from = None
    if dedup.soft_warning_contacts and not campaign.latex_enabled:
        matches = archive.find_matches_for_company(archive_dir, values.get("company", ""))
        if matches:
            choice = _offer_resume_reuse(matches, read_line=read_line)
            if choice is not None:
                try:
                    workdir = recipient_workdir(campaign.name, recipient)
                    attachment_path = archive.copy_reused_resume(choice, workdir, campaign.attachment_name)
                    reused_from = choice.name
                except archive.ArchiveError as e:
                    # Fail loud for THIS recipient only (never sys.exit —
                    # cmd_send's while-loop must keep going for the rest of
                    # the batch, same one-recipient blast radius as every
                    # other failure path in this function).
                    display.error(f"\n⚠ Could not reuse {choice.name}: {e}")
                    print("Skipped.")
                    return

    # HARD REQUIREMENT: subject/body/stage_bodies below are the exact values
    # later passed to stage_recipient() (the real send path). preview_panel()
    # only reads them to print a display-only rendering — it never wraps,
    # mutates, or returns a styled version of these variables, so no ANSI
    # code can ever reach the template-filled message that gets staged/sent.
    #
    # fill_values merges the config-sourced signature into the same fill
    # context as the drop-parsed values (see slap.templates.
    # merge_config_values) — `values` itself is left untouched, since the
    # company/role/req_id lookups below (for the résumé archive) still read
    # from it directly.
    fill_values = merge_config_values(values, signature=signature)
    subject = fill_template(campaign.subject_template, fill_values, campaign.fields)
    body = fill_template(campaign.body_template, fill_values, campaign.fields)
    stage_bodies = [fill_template(s, fill_values, campaign.fields) for s in campaign.stage_bodies]

    # Per-recipient follow-up override (post-launch): asked BEFORE the preview
    # so the preview's own cadence line always reflects exactly what will be
    # staged, never the persona's untruncated default. cadence/stage_bodies
    # are always a PREFIX of the persona's full lists — a cadence is an
    # ordered day-offset sequence, not a set of independently-selectable
    # stages.
    followup_count = _ask_followup_count(campaign, read_line=read_line)
    effective_cadence = campaign.cadence[:followup_count]
    effective_stage_bodies = stage_bodies[:followup_count]

    _warn_empty_fields(campaign, values)
    display.preview_panel(recipient, subject, body)
    if reused_from:
        print(f"Attachment: reused from {reused_from}")
    else:
        print(f"Attachment: {campaign.attachment_name}")
    if followup_count == len(campaign.cadence):
        print(f"Cadence (persona={campaign.persona}): {effective_cadence}")
    else:
        print(f"Cadence: {effective_cadence} ({followup_count} follow-up"
              f"{'s' if followup_count != 1 else ''}, persona={campaign.persona} "
              f"default is {campaign.cadence})")

    # Defaults to YES: by this point the drop has been pasted, previewed, and
    # any dedup/résumé warnings cleared — staging is the expected next step, so
    # a bare Enter proceeds; only an explicit n/no skips this recipient.
    if read_line("\nStage this send? [Y/n]: ").strip().lower() in ("n", "no"):
        print("Skipped.")
        return

    stage_recipient(
        conn, campaign=campaign.name, recipient=recipient, persona=campaign.persona,
        cadence=effective_cadence, subject=subject, body=body, stage_bodies=effective_stage_bodies,
        attachment_path=attachment_path, attachment_name=campaign.attachment_name,
        latex_enabled=campaign.latex_enabled,
        company=values.get("company", ""), role=values.get("role_catted", ""),
        req_id=values.get("req_id", ""),
        name=values.get(campaign.name_field, "") if campaign.name_field else "",
        archive_dir=archive_dir,
        field_values=values,
    )
    display.success(f"Staged {recipient}.")


def cmd_runner(args):
    try:
        global_config = load_global_config()
    except ConfigError as e:
        display.fail(f"slap: {e}")
        sys.exit(1)
    if not runner.is_active_day(global_config.schedule):
        display.success(
            f"{date.today():%A} is not an active day (active_days={global_config.schedule.active_days}) "
            f"— exiting without draining."
        )
        return
    conn = tracking.connect()
    runner.wait_for_fire_window(global_config.schedule)
    result = runner.drain(conn, global_config, os.environ.get(global_config.api_key_env, ""))
    _print_drain_result(result)


def cmd_sync(args):
    """The hourly background refresh of the dashboard's Redis-backed GMass-
    data cache (post-launch feature, slap/gmass_cache.py) — invoked by
    launchd (see `slap.py plist --job sync` / LAUNCHD.md), same as `runner`
    is for drains. Always ATTEMPTS a refresh (unlike the dashboard's own
    on-open fallback, which only refreshes when the cache is actually
    stale) — that's the entire point of a scheduled job — but still goes
    through the SAME shared lock as that fallback, so the two can never
    both run a refresh at once."""
    try:
        global_config = load_global_config()
        consumer_domains = domains.load_consumer_domains(Path(global_config.consumer_domains_file))
    except (ConfigError, domains.DomainsError) as e:
        display.fail(f"slap: {e}")
        sys.exit(1)

    api_key = os.environ.get(global_config.api_key_env, "").strip()
    if not api_key:
        display.fail(f"slap: {global_config.api_key_env} is not set — sync needs it to poll GMass. "
                     f"See .env.example.")
        sys.exit(1)

    conn = tracking.connect()
    redis_client = gmass_cache.redis_client_from_url(global_config.redis_url)

    def do_refresh():
        return dashboard.compute_gmass_dependent_data(conn, api_key, consumer_domains)

    try:
        result = gmass_cache.refresh_with_lock(redis_client, do_refresh)
    except gmass_cache.RedisUnavailable as e:
        display.fail(f"slap: Redis unreachable at {global_config.redis_url} ({e}) — "
                     f"run `slap.py doctor` for details.")
        sys.exit(1)

    if result is None:
        display.success("Another refresh was already in progress — skipped, nothing to do.")
        return

    sr = result["sync_result"]
    display.success(f"Synced: +{sr['new_replies']} replies, +{sr['new_clicks']} clicks, "
                     f"+{sr['new_bounces']} bounces. Cache updated.")
    for err in sr["errors"]:
        display.error(f"  sync error: {err}")


def cmd_plist(args):
    try:
        global_config = load_global_config()
    except ConfigError as e:
        display.fail(f"slap: {e}")
        sys.exit(1)
    if args.job == "sync":
        print(launchd.render_sync_plist(Path.cwd(), sys.executable), end="")
    else:
        print(launchd.render_plist(global_config, Path.cwd(), sys.executable), end="")


def _print_drain_result(result):
    if not result.ran:
        display.error(f"Preflight failed: {result.preflight_error}. Wrote run_failed; queue is untouched.")
        return
    message = (f"Drain complete: {result.sent} sent, {result.failed} failed, "
               f"{result.remaining_queued} still queued.")
    if result.failed:
        display.error(message)
    else:
        display.success(message)


def cmd_dashboard(args):
    try:
        global_config = load_global_config()
        consumer_domains = domains.load_consumer_domains(Path(global_config.consumer_domains_file))
    except (ConfigError, domains.DomainsError) as e:
        display.fail(f"slap: {e}")
        sys.exit(1)

    api_key = os.environ.get(global_config.api_key_env, "").strip()
    if not api_key:
        display.fail(f"slap: {global_config.api_key_env} is not set — the dashboard's on-open "
                     f"GMass poll (replies/clicks/bounces) needs it. See .env.example.")
        sys.exit(1)

    # The dashboard is now a React SPA served from a built bundle (slap/static/
    # dist/). Fail loud with the exact build command if it's missing, same "run
    # this setup step first" convention as a missing .env/config.yaml — the
    # bundle is generated, not committed (see .gitignore).
    if not (dashboard.STATIC_DIST / "index.html").exists():
        display.fail("slap: frontend bundle not built — run "
                     "`npm --prefix slap/frontend run build` first (Node required; see README).")
        sys.exit(1)

    tracking.connect().close()  # ensure the DB file + schema exist before serving
    app = dashboard.create_app(tracking.DB_PATH, global_config, consumer_domains, api_key)
    # Not 5000: macOS's AirPlay Receiver (Control Center) listens there by
    # default on every Mac since Monterey and silently intercepts requests
    # with its own 403 page, making the dashboard look broken when it's
    # actually running fine — a real, commonly-hit conflict, not a guess.
    display.success("Dashboard running at http://127.0.0.1:5050 — Ctrl-C to stop.")
    app.run(host="127.0.0.1", port=5050)


def cmd_doctor(args):
    try:
        global_config = load_global_config()
    except ConfigError as e:
        display.error(f"config.yaml: FAIL — {e}")
        sys.exit(1)
    display.success("config.yaml: OK")

    if doctor.print_report(global_config):
        display.success("\nAll checks passed.")
    else:
        sys.exit(1)


def cmd_init(args):
    try:
        init.run_init()
    except init.InitError as e:
        display.fail(f"slap init: {e}")
        sys.exit(1)


def cmd_onboard_campaign(args):
    try:
        onboard.run_onboard_campaign()
    except onboard.OnboardError as e:
        display.fail(f"slap onboard-campaign: {e}")
        sys.exit(1)


def cmd_domains(args):
    try:
        consumer_domains = domains.load_consumer_domains()
    except domains.DomainsError as e:
        display.fail(f"slap: {e}")
        sys.exit(1)

    conn = tracking.connect()
    index = domains.domain_index(conn)
    if not index:
        print("No contacts tracked yet.")
        return

    for domain in sorted(index):
        tag = " (consumer)" if domain in consumer_domains else ""
        contacts = index[domain]
        print(f"{domain}{tag} — {len(contacts)} contact(s)")
        for ctx in contacts:
            state = "replied" if ctx.replied_at else ctx.status
            print(f"  {ctx.recipient}  campaign={ctx.campaign}  {state}  first_sent={ctx.first_sent_at}")


def cmd_rebuild(args):
    conn = tracking.connect()
    event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    tracking.rebuild(conn)
    recipient_count = conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0]
    display.success(f"Rebuilt recipients cache ({recipient_count} recipients) from {event_count} events.")


def cmd_interaction(args):
    # The CLI backend for the dashboard's LinkedIn reply-gate and follow-up
    # "mark followed up" action (every dashboard write has a terminal command —
    # slap stays GUI-agnostic/TUI-friendly).
    conn = tracking.connect()
    try:
        if args.channel == "linkedin-reply":
            # Marking LinkedIn-replied now HALTS GMass outreach (status
            # 'linkedin-gate'), so it fires a real GMass unsubscribe and needs
            # the api key. One-way, like Stop — --off can't clear it.
            if args.off:
                display.error("linkedin-reply is a one-way gate (like Stop) — --off can't clear it.")
                return 1
            global_config = load_global_config()
            api_key = os.environ.get(global_config.api_key_env, "").strip()
            if not api_key:
                display.error(f"{global_config.api_key_env} is not set — the LinkedIn gate halts "
                              f"GMass outreach and needs it. See .env.example.")
                return 1
            dashboard.gate_linkedin(conn, args.recipient, api_key=api_key)
            display.success(
                f"Marked {args.recipient} LinkedIn-replied — GMass outreach halted (status linkedin-gate)."
            )
        else:  # followed-up
            dashboard.mark_followed_up(conn, args.recipient)
            display.success(f"Recorded follow-up with {args.recipient} — reminder timer reset.")
    except ValueError as e:
        display.error(str(e))
        return 1
    except Exception as e:  # GMass suppression failed → nothing recorded, fail loud
        display.error(f"GMass suppression call failed, nothing was recorded: {e}")
        return 1


def cmd_remind(args):
    # CLI backend for the dashboard's Remind action (req 9): queue a one-shot
    # follow-up that fires as a threaded reply on the next drain. Templates are
    # authored content on disk (followups/*.txt).
    conn = tracking.connect()

    if args.list:
        saved = followups.discover_followups()
        if not saved:
            display.plain("No saved follow-ups yet — author one with `remind <recipient> --new --title \"...\"`.")
            return
        display.plain("Saved follow-ups:")
        for f in saved:
            display.plain(f"  {f['slug']}: {f['title']}")
        return

    if not args.recipient:
        display.error("A recipient is required (or use --list).")
        return 1

    used_slug = None
    if args.use:
        try:
            body = followups.load_followup(args.use)["body"]
            used_slug = args.use
        except followups.FollowupError as e:
            display.fail(f"slap: {e}")
            return 1
    elif args.new:
        try:
            global_config = load_global_config()
        except ConfigError as e:
            display.fail(f"slap: {e}")
            sys.exit(1)
        editor_check = doctor.check_editor(global_config)
        if not editor_check.ok:
            display.fail(f"slap: {editor_check.detail} — set a valid `editor:` in config.yaml")
            sys.exit(1)
        scratch = recipient_workdir(CUSTOM_CAMPAIGN, args.recipient) / "remind-draft.txt"
        if not scratch.exists():
            scratch.write_text("", encoding="utf-8")
        print("\nOpening your editor to write the reminder body...")
        _open_in_editor(global_config.editor, scratch)
        body = scratch.read_text(encoding="utf-8")
        if not body.strip():
            display.error("Empty reminder body — nothing queued.")
            return 1
        if args.title:
            # "Save and send": warn that a saved template is generic and should
            # be edited before reuse (matches the dashboard's save-and-send copy).
            try:
                saved = followups.save_followup(args.title, body)
                used_slug = saved["slug"]
                display.success(f"Saved follow-up '{saved['slug']}' (edit before reusing — it's generic).")
            except followups.FollowupError as e:
                display.fail(f"slap: {e}")
                return 1
    else:
        display.error("Choose --use <slug> or --new (see `remind --list`).")
        return 1

    try:
        dashboard.queue_remind_for(conn, args.recipient, body, followup=used_slug)
    except (ValueError, QueueError) as e:
        display.error(str(e))
        return 1
    display.success(f"Queued a reminder for {args.recipient} — it fires on the next drain.")


RELOAD_SAMPLE_DIFF_COUNT = 3


def _reload_diff_lines(change) -> list:
    def render(subject, body, stage_bodies):
        lines = [f"Subject: {subject}", "", *body.splitlines()]
        for i, sb in enumerate(stage_bodies, start=1):
            lines += ["", f"--- stage {i} ---", *sb.splitlines()]
        return lines

    old_lines = render(change.old_subject, change.old_body, change.old_stage_bodies)
    new_lines = render(change.new_subject, change.new_body, change.new_stage_bodies)
    return list(difflib.unified_diff(
        old_lines, new_lines, fromfile="staged (current)", tofile="reloaded (new)", lineterm="",
    ))


def _print_reload_diff(change) -> None:
    print(f"\n{change.recipient}  (campaign={change.campaign})")
    diff_lines = _reload_diff_lines(change)
    if not diff_lines:
        print("  (no visible line-level diff)")
        return
    for line in diff_lines:
        if line.startswith("+") and not line.startswith("+++"):
            display.success(f"  {line}")
        elif line.startswith("-") and not line.startswith("---"):
            display.error(f"  {line}")
        else:
            print(f"  {line}")


def cmd_template_reload(args):
    try:
        global_config = load_global_config()
    except ConfigError as e:
        display.fail(f"slap: {e}")
        sys.exit(1)

    conn = tracking.connect()
    plan = reload.scan(conn, global_config)
    total = len(plan.changed) + len(plan.unchanged) + len(plan.failures)

    if total == 0:
        print("Nothing queued — nothing to reload.")
        reload.write_failures([])
        return

    campaigns_changed = sorted({c.campaign for c in plan.changed})
    print(f"Checked {total} queued recipient(s) across the whole queue:")
    print(f"  {len(plan.changed)} changed"
          + (f" (campaign(s): {', '.join(campaigns_changed)})" if campaigns_changed else ""))
    print(f"  {len(plan.unchanged)} already match their current templates (no-op)")
    print(f"  {len(plan.failures)} failed — left untouched")

    if plan.failures:
        display.warn(f"\n⚠ {len(plan.failures)} recipient(s) could not be reloaded:")
        for f in plan.failures:
            display.warn(f"  {f.recipient}  campaign={f.campaign}  — {f.reason}")

    # Recorded regardless of what happens next (see slap.reload's module
    # docstring): this run's failures ARE "the most recent run"'s, whether or
    # not the owner goes on to confirm applying the successful changes below.
    reload.write_failures(plan.failures)

    if not plan.changed:
        print("\nNo content changes to apply.")
        return

    sample = plan.changed[:RELOAD_SAMPLE_DIFF_COUNT]
    print(f"\nSample diff{'s' if len(sample) != 1 else ''} ({len(sample)} of {len(plan.changed)} changed):")
    for c in sample:
        _print_reload_diff(c)

    if input(f"\nApply {len(plan.changed)} change(s) to staged content? [y/N]: ").strip().lower() != "y":
        print("Skipped — no staged content changed.")
        return

    apply_failures = reload.apply_changes(plan.changed)
    applied = len(plan.changed) - len(apply_failures)
    if apply_failures:
        # Extremely rare (see apply_changes's own docstring — something
        # external interfered between scan() and this confirm), but the
        # report already written above must still reflect it: "the most
        # recent run"'s failures include these too, not just the ones scan()
        # found before the owner even confirmed.
        reload.write_failures(plan.failures + apply_failures)
        display.warn(f"\n⚠ {len(apply_failures)} recipient(s) failed while applying (left untouched):")
        for f in apply_failures:
            display.warn(f"  {f.recipient}  campaign={f.campaign}  — {f.reason}")

    display.success(f"\nReloaded {applied} recipient(s) against current templates.")


def cmd_cleanup(args):
    try:
        global_config = load_global_config()
    except ConfigError as e:
        display.fail(f"slap: {e}")
        sys.exit(1)

    conn = tracking.connect()
    report = find_cleanup_candidates(conn, global_config, min_days_idle=args.min_days_idle)

    if not report.eligible:
        print("No stale PDFs eligible for cleanup.")
    else:
        heading = "Deleted" if args.confirm else "Would delete (dry run — pass --confirm to actually delete)"
        print(f"{heading}:")
        for c in report.eligible:
            print(f"  {c.campaign}/{c.recipient}  {c.pdf_path.name}  — {c.reason}")

    if report.undetermined:
        display.warn(f"\n⚠ {len(report.undetermined)} recipient(s) skipped — state could not be determined:")
        for u in report.undetermined:
            display.warn(f"  {u.campaign}/{u.recipient}  — {u.reason}")

    if report.archived:
        display.warn(f"\n⚠ {len(report.archived)} PDF(s) kept — still referenced by a résumé archive symlink:")
        for a in report.archived:
            display.warn(f"  {a.campaign}/{a.recipient}  {a.pdf_path.name}  — {a.reason}")

    if args.confirm and report.eligible:
        deleted = delete_eligible(report.eligible)
        display.success(f"\nDeleted {len(deleted)} PDF(s) (+ .hash sidecars). resume.tex kept for all.")


def cmd_bounced(args):
    """Bounce remediation (post-launch), TUI counterpart to the Reach-outs
    page's per-row "Resend to corrected address" action — both call the
    exact same slap.queue.resend_bounced(), see that function's own
    docstring for the recovery/fallback logic. Interactive, matching
    cmd_send's own style: one bounced recipient at a time, corrected address
    prompted, dedup surfaced as information only (never a blocking
    confirm — the owner already made an explicit correction by typing an
    address), then a plain y/N to actually stage it."""
    try:
        global_config = load_global_config()
    except ConfigError as e:
        display.fail(f"slap: {e}")
        sys.exit(1)
    try:
        consumer_domains = domains.load_consumer_domains(Path(global_config.consumer_domains_file))
    except domains.DomainsError as e:
        display.fail(f"slap: {e}")
        sys.exit(1)

    conn = tracking.connect()
    bounced = dashboard.bounces(conn)
    if not bounced:
        print("No bounces or blocks to fix.")
        return

    archive_dir = archive.archive_dir_from_env()

    for b in bounced:
        recipient, campaign = b["recipient"], b["campaign"]
        kind = "Blocked" if b["category"] == "block" else "Bounced"
        print(f"\n{kind}: {recipient}  (campaign={campaign})")
        corrected = input(display.styled_prompt(
            "Corrected email address [blank to skip]: ", style=display.YELLOW
        )).strip()
        if not corrected:
            print("Skipped.")
            continue

        dedup = domains.check_recipient(conn, corrected, consumer_domains)
        if dedup.hard_warning:
            w = dedup.hard_warning
            display.error(f"⚠ HARD WARN: {corrected} already contacted — campaign={w.campaign} "
                          f"status={w.status}")
        if dedup.soft_warning_contacts:
            display.warn(f"⚠ SOFT WARN: {len(dedup.soft_warning_contacts)} other contact(s) already on "
                         f"domain {dedup.soft_warning_domain}")

        if input("Resend the full sequence to this address? [y/N]: ").strip().lower() != "y":
            print("Skipped.")
            continue

        try:
            resend_bounced(conn, original_recipient=recipient, corrected_email=corrected,
                            archive_dir=archive_dir)
        except AmbiguousArchiveChoice as e:
            # The workdir's own compiled PDF is gone (cleaned up) but the
            # archive has candidate(s) for this company — offer the exact
            # same numbered picker _prep_one_recipient's résumé-reuse flow
            # already uses, rather than silently guessing one (see
            # resend_bounced's own docstring for why it never auto-picks).
            choice = _offer_resume_reuse(e.matches)
            if choice is None:
                display.error(f"⚠ Could not resend for {recipient}: no résumé chosen, nothing staged.")
                continue
            try:
                resend_bounced(conn, original_recipient=recipient, corrected_email=corrected,
                                archive_dir=archive_dir, archive_choice=choice)
            except QueueError as e2:
                display.error(f"⚠ Could not resend for {recipient}: {e2}")
                continue
        except QueueError as e:
            display.error(f"⚠ Could not resend for {recipient}: {e}")
            continue
        display.success(f"Staged {corrected} (corrected from {recipient}).")


def build_parser():
    parser = argparse.ArgumentParser(description="Personal cold job-outreach CLI over GMass.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List auto-discovered campaigns").set_defaults(func=cmd_list)

    p_send = sub.add_parser(
        "send", help="Prep flow: stage a recipient to the queue (use `send custom` for a one-off editor-authored send)"
    )
    p_send.add_argument("campaign", help="Campaign name, or the literal 'custom' for an editor-authored one-off send")
    p_send.add_argument("--now", action="store_true", help="Also drain immediately after staging")
    p_send.set_defaults(func=cmd_send)

    sub.add_parser("dashboard", help="Launch the localhost dashboard").set_defaults(func=cmd_dashboard)
    sub.add_parser("doctor", help="Run preflight checks").set_defaults(func=cmd_doctor)
    sub.add_parser(
        "init", help="Interactive installer — config.yaml, .env, schedule, DB, launchd"
    ).set_defaults(func=cmd_init)
    sub.add_parser(
        "onboard-campaign", help="Interactive wizard to scaffold a new campaigns/<name>/ folder"
    ).set_defaults(func=cmd_onboard_campaign)
    sub.add_parser("domains", help="Regenerate/print the domain index").set_defaults(func=cmd_domains)
    sub.add_parser("rebuild", help="Rebuild the recipients cache from events").set_defaults(func=cmd_rebuild)

    p_interaction = sub.add_parser(
        "interaction", help="Record a per-reachout interaction (LinkedIn-replied / followed-up)"
    )
    p_interaction.add_argument("recipient")
    p_interaction.add_argument("--channel", choices=["linkedin-reply", "followed-up"], required=True,
                                help="linkedin-reply: mark replied-on-LinkedIn and HALT GMass outreach "
                                     "(status linkedin-gate; one-way, like Stop); followed-up: reset the reminder timer")
    p_interaction.add_argument("--off", action="store_true",
                                help="(deprecated) linkedin-reply is a one-way gate and can't be cleared")
    p_interaction.set_defaults(func=cmd_interaction)

    p_remind = sub.add_parser(
        "remind", help="Queue a one-shot follow-up reminder (fires as a threaded reply on the next drain)"
    )
    p_remind.add_argument("recipient", nargs="?", help="Recipient to remind (omit when using --list)")
    p_remind.add_argument("--list", action="store_true", help="List saved follow-up templates")
    p_remind.add_argument("--use", metavar="SLUG", help="Send using a saved follow-up template")
    p_remind.add_argument("--new", action="store_true", help="Author a new reminder body in your editor")
    p_remind.add_argument("--title", help="With --new: also save the authored body as a reusable template")
    p_remind.set_defaults(func=cmd_remind)
    sub.add_parser(
        "template-reload",
        help="Re-render every not-yet-sent recipient's staged content against current templates",
    ).set_defaults(func=cmd_template_reload)
    sub.add_parser(
        "runner", help="Unattended drain — invoked by launchd, see LAUNCHD.md"
    ).set_defaults(func=cmd_runner)
    sub.add_parser(
        "sync", help="Hourly GMass-data cache refresh — invoked by launchd, see LAUNCHD.md"
    ).set_defaults(func=cmd_sync)

    p_plist = sub.add_parser(
        "plist", help="Print the launchd .plist for the unattended runner (or --job sync), see LAUNCHD.md"
    )
    p_plist.add_argument("--job", choices=["runner", "sync"], default="runner",
                          help="Which job's plist to print (default: runner)")
    p_plist.set_defaults(func=cmd_plist)

    p_cleanup = sub.add_parser(
        "cleanup", help="Delete stale compiled PDFs for done/dead/no-reply recipients (dry run by default)"
    )
    p_cleanup.add_argument("--confirm", action="store_true", help="Actually delete (default is dry run)")
    p_cleanup.add_argument("--min-days-idle", type=int, default=DEFAULT_MIN_DAYS_IDLE, dest="min_days_idle",
                            help=f"Idle-days threshold (default {DEFAULT_MIN_DAYS_IDLE})")
    p_cleanup.set_defaults(func=cmd_cleanup)

    sub.add_parser(
        "bounced", help="Fix bounced/blocked recipients — prompt a corrected address and resend"
    ).set_defaults(func=cmd_bounced)
    return parser


def main(argv=None):
    # Loaded here (the CLI entry point), NOT at module import time: importing
    # slap.py for introspection only (slap.api._load_cli() walks build_parser()
    # to derive the dashboard's Commands reference) must never mutate the
    # calling process's os.environ by loading a real .env — that side effect
    # once leaked RESUME_ARCHIVE_DIR into the test/dashboard process. Every real
    # CLI invocation still goes through main(), so behavior is unchanged.
    load_dotenv()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
