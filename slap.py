#!/usr/bin/env python3
"""slap.py - personal cold job-outreach CLI over the GMass API.

See SLAP_BUILD_PROMPT.md for the full spec and CONTROL_SHEET.md for the
current build state / package layout.
"""
import argparse
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from slap import display
from slap.cleanup import DEFAULT_MIN_DAYS_IDLE, delete_eligible, find_cleanup_candidates
from slap.config import ConfigError, discover_campaigns, load_campaign, load_global_config
from slap.latex import recipient_workdir, run_latex_loop
from slap.queue import stage_recipient
from slap.templates import fill_template, parse_drop
from slap import archive, dashboard, doctor, domains, gmass, init, launchd, runner, tracking

load_dotenv()

PASTE_TERMINATOR = "EOF"


def read_paste(prompt: str, read_line=input) -> str:
    """Reads a multi-line paste terminated by a line containing only
    PASTE_TERMINATOR, not a blocking read-until-EOF. A real read-until-EOF
    (sys.stdin.read()) would consume the entire stdin stream, leaving
    nothing for any later input() prompt (Add another?, confirmations) to
    read — this works correctly for both a live terminal and piped/scripted
    input, and doesn't rely on TTY-specific Ctrl-D-per-read semantics."""
    print(f"{prompt} (end with a line containing only {PASTE_TERMINATOR}):")
    lines = []
    while True:
        try:
            line = read_line()
        except EOFError:
            break
        if line.strip() == PASTE_TERMINATOR:
            break
        lines.append(line)
    return "\n".join(lines)


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
            _prep_one_recipient(conn, campaign, consumer_domains, values, recipient, archive_dir)

        if input("\nAdd another? [Y/n]: ").strip().lower() == "n":
            break

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


def _prep_one_recipient(conn, campaign, consumer_domains, values, recipient, archive_dir):
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
            input(display.styled_prompt("Proceed anyway? [y/N]: ", style=display.YELLOW)).strip().lower() != "y":
        print("Skipped.")
        return

    # HARD REQUIREMENT: subject/body/stage_bodies below are the exact values
    # later passed to stage_recipient() (the real send path). preview_panel()
    # only reads them to print a display-only rendering — it never wraps,
    # mutates, or returns a styled version of these variables, so no ANSI
    # code can ever reach the template-filled message that gets staged/sent.
    subject = fill_template(campaign.subject_template, values, campaign.fields)
    body = fill_template(campaign.body_template, values, campaign.fields)
    stage_bodies = [fill_template(s, values, campaign.fields) for s in campaign.stage_bodies]

    _warn_empty_fields(campaign, values)
    display.preview_panel(recipient, subject, body)
    print(f"Attachment: {campaign.attachment_name}")
    print(f"Cadence (persona={campaign.persona}): {campaign.cadence}")

    if input("\nStage this send? [y/N]: ").strip().lower() != "y":
        print("Skipped.")
        return

    stage_recipient(
        conn, campaign=campaign.name, recipient=recipient, persona=campaign.persona,
        cadence=campaign.cadence, subject=subject, body=body, stage_bodies=stage_bodies,
        attachment_path=attachment_path, attachment_name=campaign.attachment_name,
        latex_enabled=campaign.latex_enabled,
        company=values.get("company", ""), role=values.get("role_catted", ""),
        archive_dir=archive_dir,
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


def cmd_plist(args):
    try:
        global_config = load_global_config()
    except ConfigError as e:
        display.fail(f"slap: {e}")
        sys.exit(1)
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

    tracking.connect().close()  # ensure the DB file + schema exist before serving
    app = dashboard.create_app(tracking.DB_PATH, global_config, consumer_domains, api_key)
    display.success("Dashboard running at http://127.0.0.1:5000 — Ctrl-C to stop.")
    app.run(host="127.0.0.1", port=5000)


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


def build_parser():
    parser = argparse.ArgumentParser(description="Personal cold job-outreach CLI over GMass.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List auto-discovered campaigns").set_defaults(func=cmd_list)

    p_send = sub.add_parser("send", help="Prep flow: stage a recipient to the queue")
    p_send.add_argument("campaign")
    p_send.add_argument("--now", action="store_true", help="Also drain immediately after staging")
    p_send.set_defaults(func=cmd_send)

    sub.add_parser("dashboard", help="Launch the localhost dashboard").set_defaults(func=cmd_dashboard)
    sub.add_parser("doctor", help="Run preflight checks").set_defaults(func=cmd_doctor)
    sub.add_parser(
        "init", help="Interactive installer — config.yaml, .env, schedule, DB, launchd"
    ).set_defaults(func=cmd_init)
    sub.add_parser("domains", help="Regenerate/print the domain index").set_defaults(func=cmd_domains)
    sub.add_parser("rebuild", help="Rebuild the recipients cache from events").set_defaults(func=cmd_rebuild)
    sub.add_parser(
        "runner", help="Unattended drain — invoked by launchd, see LAUNCHD.md"
    ).set_defaults(func=cmd_runner)
    sub.add_parser(
        "plist", help="Print the launchd .plist for the unattended runner, see LAUNCHD.md"
    ).set_defaults(func=cmd_plist)

    p_cleanup = sub.add_parser(
        "cleanup", help="Delete stale compiled PDFs for done/dead/no-reply recipients (dry run by default)"
    )
    p_cleanup.add_argument("--confirm", action="store_true", help="Actually delete (default is dry run)")
    p_cleanup.add_argument("--min-days-idle", type=int, default=DEFAULT_MIN_DAYS_IDLE, dest="min_days_idle",
                            help=f"Idle-days threshold (default {DEFAULT_MIN_DAYS_IDLE})")
    p_cleanup.set_defaults(func=cmd_cleanup)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
