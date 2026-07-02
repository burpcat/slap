#!/usr/bin/env python3
"""slap.py - personal cold job-outreach CLI over the GMass API.

See SLAP_BUILD_PROMPT.md for the full spec and CONTROL_SHEET.md for the
current build state / package layout.
"""
import argparse
import sys


def cmd_list(args):
    sys.exit(
        "slap: 'list' is not yet implemented — Build Order step 3 "
        "(config loader + campaign auto-discovery). See SLAP_BUILD_PROMPT.md §14."
    )


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
    sys.exit(
        "slap: 'domains' is not yet implemented — Build Order step 7 "
        "(domain/recipient dedup + domains command). See SLAP_BUILD_PROMPT.md §14."
    )


def cmd_rebuild(args):
    sys.exit(
        "slap: 'rebuild' is not yet implemented — Build Order step 5 "
        "(tracking store + rebuild). See SLAP_BUILD_PROMPT.md §14."
    )


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
