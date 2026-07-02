#!/usr/bin/env python3
"""slap.py - personal cold job-outreach CLI over the GMass API.

See SLAP_BUILD_PROMPT.md for the full spec and CONTROL_SHEET.md for the
current build state / package layout.
"""
import argparse
import sys

from slap.config import ConfigError, discover_campaigns, load_campaign, load_global_config
from slap import domains, tracking


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


def cmd_send(args):
    sys.exit(
        "slap: 'send' is not yet implemented — depends on Build Order steps 3-9 "
        "(config, templates, tracking, GMass client, domain dedup, LaTeX loop, "
        "queue+runner). See SLAP_BUILD_PROMPT.md §14."
    )


def cmd_dashboard(args):
    sys.exit(
        "slap: 'dashboard' is not yet implemented — Build Order step 11 "
        "(localhost dashboard). See SLAP_BUILD_PROMPT.md §14."
    )


def cmd_doctor(args):
    sys.exit(
        "slap: 'doctor' is not yet implemented — Build Order step 12 "
        "(doctor preflight wiring). See SLAP_BUILD_PROMPT.md §14."
    )


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
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
