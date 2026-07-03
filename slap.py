#!/usr/bin/env python3
"""slap.py - personal cold job-outreach CLI over the GMass API.

See SLAP_BUILD_PROMPT.md for the full spec and CONTROL_SHEET.md for the
current build state / package layout.
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from slap.config import ConfigError, discover_campaigns, load_campaign, load_global_config
from slap.latex import recipient_workdir, run_latex_loop
from slap.queue import stage_recipient
from slap.templates import fill_template, parse_drop
from slap import dashboard, doctor, domains, gmass, runner, tracking

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
        sys.exit(f"slap: {e}")

    names = discover_campaigns()
    if not names:
        print("No campaigns found under campaigns/.")
        return

    for name in names:
        try:
            campaign = load_campaign(name, global_config)
        except ConfigError as e:
            print(f"{name}: ERROR — {e}")
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
        sys.exit(f"slap: doctor preflight failed — run `slap.py doctor` for details:\n{lines}")


def cmd_send(args):
    try:
        global_config = load_global_config()
        campaign = load_campaign(args.campaign, global_config)
    except ConfigError as e:
        sys.exit(f"slap: {e}")

    _run_doctor_or_exit(global_config, campaign)

    try:
        consumer_domains = domains.load_consumer_domains(Path(global_config.consumer_domains_file))
    except domains.DomainsError as e:
        sys.exit(f"slap: {e}")

    conn = tracking.connect()

    while True:
        drop_text = read_paste(f"\nPaste the drop for campaign '{campaign.name}'")
        values = parse_drop(drop_text, campaign.fields)

        recipient = values.get("email", "").strip()
        if not recipient:
            print("No 'Email' value found in the drop — skipping this recipient.")
        else:
            _prep_one_recipient(conn, campaign, consumer_domains, values, recipient)

        if input("\nAdd another? [Y/n]: ").strip().lower() == "n":
            break

    if args.now:
        print("\n--now: draining the queue immediately...")
        result = runner.drain(conn, global_config, os.environ.get(global_config.api_key_env, ""))
        _print_drain_result(result)


def _prep_one_recipient(conn, campaign, consumer_domains, values, recipient):
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
        print(f"\n⚠ HARD WARN: {recipient} already contacted — campaign={w.campaign} "
              f"status={w.status} first_sent={w.first_sent_at} replied={replied}")
    if dedup.soft_warning_contacts:
        print(f"\n⚠ SOFT WARN: {len(dedup.soft_warning_contacts)} other contact(s) already on "
              f"domain {dedup.soft_warning_domain}:")
        for c in dedup.soft_warning_contacts:
            print(f"    {c.recipient}  campaign={c.campaign}  status={c.status}")
    if (dedup.hard_warning or dedup.soft_warning_contacts) and \
            input("Proceed anyway? [y/N]: ").strip().lower() != "y":
        print("Skipped.")
        return

    subject = fill_template(campaign.subject_template, values, campaign.fields)
    body = fill_template(campaign.body_template, values, campaign.fields)
    stage_bodies = [fill_template(s, values, campaign.fields) for s in campaign.stage_bodies]

    print(f"\n--- Preview for {recipient} ---\nSubject: {subject}\n\n{body}\n")
    print(f"Attachment: {campaign.attachment_name}")
    print(f"Cadence (persona={campaign.persona}): {campaign.cadence}")

    if input("\nStage this send? [y/N]: ").strip().lower() != "y":
        print("Skipped.")
        return

    stage_recipient(
        conn, campaign=campaign.name, recipient=recipient, persona=campaign.persona,
        cadence=campaign.cadence, subject=subject, body=body, stage_bodies=stage_bodies,
        attachment_path=attachment_path, attachment_name=campaign.attachment_name,
    )
    print(f"Staged {recipient}.")


def cmd_runner(args):
    try:
        global_config = load_global_config()
    except ConfigError as e:
        sys.exit(f"slap: {e}")
    conn = tracking.connect()
    runner.wait_for_fire_window(global_config.schedule)
    result = runner.drain(conn, global_config, os.environ.get(global_config.api_key_env, ""))
    _print_drain_result(result)


def _print_drain_result(result):
    if not result.ran:
        print(f"Preflight failed: {result.preflight_error}. Wrote run_failed; queue is untouched.")
        return
    print(f"Drain complete: {result.sent} sent, {result.failed} failed, "
          f"{result.remaining_queued} still queued.")


def cmd_dashboard(args):
    try:
        global_config = load_global_config()
        consumer_domains = domains.load_consumer_domains(Path(global_config.consumer_domains_file))
    except (ConfigError, domains.DomainsError) as e:
        sys.exit(f"slap: {e}")

    api_key = os.environ.get(global_config.api_key_env, "").strip()
    if not api_key:
        sys.exit(f"slap: {global_config.api_key_env} is not set — the dashboard's on-open "
                  f"GMass poll (replies/clicks/bounces) needs it. See .env.example.")

    tracking.connect().close()  # ensure the DB file + schema exist before serving
    app = dashboard.create_app(tracking.DB_PATH, global_config, consumer_domains, api_key)
    print("Dashboard running at http://127.0.0.1:5000 — Ctrl-C to stop.")
    app.run(host="127.0.0.1", port=5000)


def _print_check(result, *, indent=""):
    if result.ok:
        suffix = f" ({result.detail})" if result.detail else ""
        print(f"{indent}{result.name}: OK{suffix}")
    else:
        print(f"{indent}{result.name}: FAIL — {result.detail}")


def cmd_doctor(args):
    try:
        global_config = load_global_config()
    except ConfigError as e:
        print(f"config.yaml: FAIL — {e}")
        sys.exit(1)
    print("config.yaml: OK")

    any_failed = False
    for result in doctor.run_global_checks(global_config):
        _print_check(result)
        any_failed = any_failed or not result.ok

    names = discover_campaigns()
    if not names:
        print("No campaigns found under campaigns/.")
    for name in names:
        try:
            campaign = load_campaign(name, global_config)
        except ConfigError as e:
            print(f"campaign '{name}': FAIL — {e}")
            any_failed = True
            continue
        campaign_results = doctor.run_campaign_checks(campaign)
        campaign_ok = all(r.ok for r in campaign_results)
        print(f"campaign '{name}': {'OK' if campaign_ok else 'FAIL'}")
        for result in campaign_results:
            _print_check(result, indent="  ")
        any_failed = any_failed or not campaign_ok

    if any_failed:
        sys.exit(1)
    print("\nAll checks passed.")


def cmd_domains(args):
    try:
        consumer_domains = domains.load_consumer_domains()
    except domains.DomainsError as e:
        sys.exit(f"slap: {e}")

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
    print(f"Rebuilt recipients cache ({recipient_count} recipients) from {event_count} events.")


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
    sub.add_parser("domains", help="Regenerate/print the domain index").set_defaults(func=cmd_domains)
    sub.add_parser("rebuild", help="Rebuild the recipients cache from events").set_defaults(func=cmd_rebuild)
    sub.add_parser(
        "runner", help="Unattended drain — invoked by launchd, see LAUNCHD.md"
    ).set_defaults(func=cmd_runner)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
