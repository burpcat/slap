"""JSON API layer over the same read/write logic slap/dashboard.py's Jinja
routes already use (Build Order step 11, post-launch). PURELY ADDITIVE: every
`/api/*` route defined here is a second, JSON-speaking front door onto the
exact same widget functions (today_strip, pipeline, reachouts_rows, ...) and
the exact same write actions (tag_reply, stop_outreach, resend_bounced, ...)
the existing Jinja pages call — never a second, independently-derived
implementation of any of them (one source of truth, same as everywhere else
in this app). A later task removes the Jinja templates/routes once a React
frontend consumes this layer instead; that cutover is NOT done here — every
existing route in dashboard.py is untouched.

Import direction: this module imports FROM slap.dashboard (widget functions,
create_app's WARM_BUT_SILENT_WIDGET constant, etc). To keep that a one-way
dependency (no cycle), slap/dashboard.py does NOT import this module at its
own top level — create_app() imports register_api lazily, inside its own
function body, right before calling it. By the time that call happens,
slap.dashboard has already finished executing top-to-bottom (create_app is
being invoked, not defined), so this module's own top-level
`from slap import dashboard` sees a fully-initialized module either way.

Timestamp/date serialization: most widget functions already return raw UTC
ISO-8601 strings for timestamps (exactly as stored in the `events` table,
per §5) or pre-formatted ISO date strings (e.g. sent_reply_trend's `date`,
bounce_breakdown's `week_start`) -- those pass through jsonify() completely
unchanged, and a browser running on the SAME machine as this localhost-only
server can format them for local display same as it always could.
A few widgets return actual `date`/`datetime` objects instead of strings
(dashboard.this_week's range_start/range_end, dashboard.pipeline's
followups_scheduled fire_date entries) -- Flask's DEFAULT JSON provider
would silently convert those to an HTTP-date string ("Tue, 28 Jul 2026
00:00:00 GMT", via werkzeug's http_date()), which is neither a stored-form
ISO string nor anything a JSON API consumer would expect. A minimal custom
JSON provider (ApiJSONProvider, below) is the fix: it overrides just the
date/datetime branch to emit plain .isoformat(), and defers to Flask's
normal default() for everything else (Decimal, UUID, dataclasses, ...) so
nothing else about JSON encoding changes. Verified safe to install
app-wide (not just under /api/*): every EXISTING `| tojson` usage in
dashboard_templates/ (analytics.html, logs.html) is already fed
pre-formatted strings, never a raw date/datetime object, so this provider
changes zero existing template output.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
from datetime import date, datetime
from pathlib import Path

from flask import jsonify, request
from flask.json.provider import DefaultJSONProvider

from slap import archive, gmass_cache, tracking, ui_state
from slap.color import campaign_colors
from slap.config import discover_campaigns
from slap.domains import check_recipient
from slap.followups import FollowupError, discover_followups, load_followup, save_followup
from slap.queue import QueueError, resend_bounced

# Cache for the lazily-loaded top-level CLI module (slap.py). See _load_cli().
_cli_module = None


def _load_cli():
    """Import the top-level `slap.py` CLI script as a module and return it.

    `slap.py` (the script) and `slap/` (this package) share the name `slap`, so
    a plain `import slap` resolves to the PACKAGE — the script is never
    importable that way. We load it by file path instead (it's a sibling of the
    package directory) so `/api/commands` can call its `build_parser()` and stay
    in lockstep with the real CLI. slap.py guards `main()` behind
    `if __name__ == "__main__"`, so importing it defines the parser/handlers
    without running anything. Cached module-level: loaded once per process."""
    global _cli_module
    if _cli_module is None:
        cli_path = Path(__file__).resolve().parent.parent / "slap.py"
        spec = importlib.util.spec_from_file_location("slap_cli", cli_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _cli_module = module
    return _cli_module


# argparse (Python 3.13+) colorizes format_usage()/help with ANSI SGR escapes
# when it thinks output is a terminal. Those escapes are meaningless in the
# JSON payload and leaked as literal "[1;34m…" noise into the Commands tab —
# strip them so the usage line renders as plain text.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s or "")


# USAGE.md is the human-authored day-to-day guide; its fenced code blocks and
# inline `slap.py …` command spans are the canonical, curated examples. We pull
# them straight from there so the Commands tab's examples match the docs (and
# update whenever the docs do) rather than being a second, hand-kept copy.
_USAGE_PATH = Path(__file__).resolve().parent.parent / "USAGE.md"
_usage_examples_cache = None


def _load_usage_examples() -> dict:
    """Parse USAGE.md into {command_name: [example invocation, ...]}. Pulls
    `slap.py <cmd> …` lines from fenced code blocks and inline `code` spans,
    keeping only clean, runnable ones — anything carrying argparse signature
    notation ([], {}, |) is skipped (that's what the `usage:` line is for).
    Cached per process; a missing USAGE.md just yields {} (callers fall back to
    synthesized examples)."""
    global _usage_examples_cache
    if _usage_examples_cache is not None:
        return _usage_examples_cache
    result: dict = {}
    try:
        text = _USAGE_PATH.read_text()
    except OSError:
        _usage_examples_cache = {}
        return _usage_examples_cache

    candidates = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            candidates.append(stripped)
        else:
            candidates.extend(re.findall(r"`([^`]+)`", line))  # inline `code` spans

    for cand in candidates:
        cand = cand.strip()
        m = re.match(r"^(?:python\s+)?slap\.py\s+([a-z][\w-]*)\b", cand)
        if not m or any(ch in cand for ch in "{}|["):
            continue
        name = m.group(1)
        ex = re.sub(r"^python\s+", "", cand)  # uniform "slap.py …"
        result.setdefault(name, [])
        if ex not in result[name]:
            result[name].append(ex)
    _usage_examples_cache = result
    return result


def _fill_placeholders(example: str, campaign: str, recipient: str) -> str:
    """Swap the doc's placeholders for the owner's real data so examples read
    like their own tool."""
    return (example
            .replace("<campaign>", campaign)
            .replace("my-campaign", campaign)
            .replace("<recipient>", recipient))


def _command_examples(name: str, args: list, campaigns: list, recipient: str) -> list:
    """Concrete, copy-pasteable example invocations for a command — using the
    owner's REAL campaign names and a real recipient from their DB (passed in)
    so the reference reads like their own tool, not abstract placeholders.
    Curated per command for the ones with interesting flags/arguments; a
    generic `slap.py <name> <required positionals>` is synthesized for the
    rest, with campaign/recipient positionals filled from real data too."""
    c0 = campaigns[0] if campaigns else "my-campaign"
    c1 = campaigns[1] if len(campaigns) > 1 else c0
    curated = {
        "list": ["slap.py list"],
        "send": [f"slap.py send {c0}", f"slap.py send {c1} --now", "slap.py send custom"],
        "runner": ["slap.py runner"],
        "sync": ["slap.py sync"],
        "dashboard": ["slap.py dashboard"],
        "doctor": ["slap.py doctor"],
        "domains": ["slap.py domains"],
        "rebuild": ["slap.py rebuild"],
        "bounced": ["slap.py bounced"],
        "cleanup": ["slap.py cleanup", "slap.py cleanup --confirm", "slap.py cleanup --min-days-idle 30"],
        "interaction": [
            f"slap.py interaction {recipient} --channel linkedin-reply",
            f"slap.py interaction {recipient} --channel followed-up",
            f"slap.py interaction {recipient} --channel linkedin-reply --off",
        ],
        "remind": [
            "slap.py remind --list",
            f"slap.py remind {recipient} --use quick-nudge",
            f'slap.py remind {recipient} --new --title "Quick nudge"',
        ],
    }
    if name in curated:
        return curated[name]
    # Generic fallback: fill required positionals with real values where the arg
    # name tells us how (campaign/recipient), else the first choice, else a
    # readable placeholder. Optional flags are left off the default example.
    parts = ["slap.py", name]
    for a in args:
        if a["flags"] or not a["required"]:
            continue
        dest = a["name"]
        if "campaign" in dest:
            parts.append(c0)
        elif "recipient" in dest:
            parts.append(recipient)
        elif a["choices"]:
            parts.append(str(a["choices"][0]))
        else:
            parts.append(f"<{dest}>")
    return [" ".join(parts)]


def commands_reference(*, campaigns=None, sample_recipient=None) -> list:
    """The terminal-command reference, derived live from the CLI's own argparse
    definition (`build_parser()` in slap.py) so it can never drift from the real
    commands — a new subcommand shows up here automatically. Reaches into
    argparse's `_SubParsersAction`/`_actions` internals (argparse exposes no
    public introspection API); a test asserts the expected command list so a
    Python-version change to those internals surfaces as a failure, not a
    silently empty Commands tab. `campaigns`/`sample_recipient` (the owner's own
    data) are woven into each command's examples so the reference feels concrete."""
    campaigns = list(campaigns or [])
    recipient = sample_recipient or "jordan@acme.com"
    c0 = campaigns[0] if campaigns else "my-campaign"
    usage_examples = _load_usage_examples()
    parser = _load_cli().build_parser()
    sub_actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    out = []
    for sub in sub_actions:
        # Per-choice help text lives on the pseudo-actions in _choices_actions.
        help_by_name = {ca.dest: (ca.help or "") for ca in sub._choices_actions}
        for name, subparser in sub.choices.items():
            args = []
            for act in subparser._actions:
                if isinstance(act, argparse._HelpAction):
                    continue
                args.append({
                    "name": act.dest,
                    "flags": list(act.option_strings),  # [] for positionals
                    "help": _strip_ansi(act.help or ""),
                    "required": bool(getattr(act, "required", False)),
                    "choices": list(act.choices) if act.choices else None,
                })
            # Prefer USAGE.md's curated examples (with the owner's real
            # campaign/recipient filled in); fall back to synthesized ones only
            # for commands the docs don't show a runnable example for.
            doc_examples = [_fill_placeholders(e, c0, recipient) for e in usage_examples.get(name, [])]
            out.append({
                "name": name,
                "help": _strip_ansi(help_by_name.get(name, "")),
                "usage": _strip_ansi(" ".join(subparser.format_usage().split())),
                "args": args,
                "examples": doc_examples or _command_examples(name, args, campaigns, recipient),
            })
    out.sort(key=lambda c: c["name"])
    return out


class ApiJSONProvider(DefaultJSONProvider):
    """See module docstring's "Timestamp/date serialization" section."""

    def default(self, o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)


def register_api(app, *, get_conn, db_path, global_config, consumer_domains, api_key, redis_client, log_dir):
    """Registers every `/api/*` JSON route on `app`, closing over the same
    per-request `get_conn()` and startup-resolved params create_app() already
    built for the Jinja routes -- mirrors that function's own closure style
    exactly (see create_app's docstring for why `get_conn` is a lazy,
    per-request, `g`-cached connection rather than one shared connection)."""
    # Imported here, not at this module's own top level, purely to keep the
    # docstring's "one-way dependency" claim trivially true even if someone
    # later reorders imports above -- see module docstring for the full
    # reasoning (this particular import has no actual cycle risk by the time
    # register_api() is ever called, but keeping the dashboard-facing import
    # local to this function makes that reasoning easy to re-verify later).
    from slap import dashboard

    app.json = ApiJSONProvider(app)

    # --- read endpoints ------------------------------------------------

    @app.route("/api/home")
    def api_home():
        # Mirrors index() (dashboard.py) plus the three widgets moved to the
        # front page in the API redesign (follow_up_reminders/pipeline/
        # companies) -- see this endpoint's own task brief for why those
        # three ride along here even though index() itself doesn't render
        # them (a later frontend collapses Home+Pipeline's overlap).
        conn = get_conn()
        gmass_data = dashboard.get_gmass_dependent_data(api_key, consumer_domains, redis_client, db_path)
        return jsonify({
            "sync_result": gmass_data["sync_result"],
            "replies": gmass_data["replies"],
            "cache_status": gmass_data["cache_status"],
            "today": dashboard.today_strip(conn, global_config),
            "week": dashboard.this_week(conn),
            "runs": dashboard.todays_runs(conn),
            "next_drain": dashboard.next_drain(conn, global_config),
            "follow_up_reminders": dashboard.follow_up_reminders(conn),
            "pipeline": dashboard.pipeline(conn, global_config),
            "companies": dashboard.companies_contacted(conn, consumer_domains),
        })

    @app.route("/api/pipeline")
    def api_pipeline():
        # Mirrors pipeline_page(), with Deliverability folded in (bounces +
        # stopped_outreach) -- Deliverability is being merged into Pipeline
        # per this endpoint's own task brief.
        conn = get_conn()
        gmass_data = dashboard.get_gmass_dependent_data(api_key, consumer_domains, redis_client, db_path)
        return jsonify({
            # today_strip rides along (cheap, GMass-independent) so the
            # "follow-ups firing today" panel can show how many already FIRED
            # today (today.sent.follow_up) vs are still due — req 2.
            "today": dashboard.today_strip(conn, global_config),
            "active_leads": dashboard.active_leads(conn),
            "follow_up_reminders": dashboard.follow_up_reminders(conn),
            "pipeline": dashboard.pipeline(conn, global_config),
            "companies": dashboard.companies_contacted(conn, consumer_domains),
            "bounces": gmass_data["bounces"],
            "stopped_outreach": dashboard.stopped_outreach_roster(conn),
        })

    @app.route("/api/engagement")
    def api_engagement():
        # Mirrors engagement_page() (warm-but-silent hide/unhide state,
        # ?show_hidden=1) plus analytics_page()'s trend/comparison charts,
        # folded in here under the "engagement-analytics" key per this
        # endpoint's own task brief (Analytics is being merged into
        # Engagement in the same redesign that merges Deliverability into
        # Pipeline, above).
        conn = get_conn()
        gmass_data = dashboard.get_gmass_dependent_data(api_key, consumer_domains, redis_client, db_path)
        all_warm_but_silent = gmass_data["warm_but_silent"]
        hidden_recipients = dashboard._warm_but_silent_hidden_recipients(conn)
        show_hidden = request.args.get("show_hidden") == "1"
        visible_warm_but_silent_rows = (
            all_warm_but_silent if show_hidden
            else [r for r in all_warm_but_silent if r["recipient"] not in hidden_recipients]
        )
        return jsonify({
            "sync_result": gmass_data["sync_result"],
            "cache_status": gmass_data["cache_status"],
            "engagement": gmass_data["engagement"],
            "warm_but_silent": visible_warm_but_silent_rows,
            "warm_but_silent_hidden_count": len(
                [r for r in all_warm_but_silent if r["recipient"] in hidden_recipients]
            ),
            "show_hidden": show_hidden,
            "engagement-analytics": {
                "trend": dashboard.sent_reply_trend(conn, days=30),
                "bounce_data": dashboard.bounce_breakdown(conn),
                "reply_rate_by_persona": dashboard._reply_rate_by_persona(conn),
                "time_to_first_reply": dashboard._time_to_first_reply_distribution(conn),
                "weekly_goal": dashboard.weekly_goal_progress(conn, global_config.schedule.weekly_target),
            },
        })

    @app.route("/api/campaigns")
    def api_campaigns():
        # discover_campaigns() (slap.config -- the one source of truth for
        # "which campaigns exist," per-campaign.yaml presence, no central
        # registry, §4) plus a cheap per-campaign slice computed by
        # filtering reachouts_rows()/active_leads() -- the SAME per-recipient
        # data every other endpoint already reuses, not a fresh aggregation.
        # Per-campaign color is derived live (slap.color, stable name->hue hash),
        # never stored — both light/dark hexes ride along so a chip/row-tint can
        # theme from one payload without a second request.
        conn = get_conn()
        campaigns = discover_campaigns()
        rows = dashboard.reachouts_rows(conn)
        leads = dashboard.active_leads(conn)

        def _slice(name):
            campaign_rows = [r for r in rows if r["campaign"] == name]
            return {
                "campaign": name,
                "color": campaign_colors(name),
                "recipient_count": len(campaign_rows),
                "reply_count": sum(1 for r in campaign_rows if r["engagement"] == "replied"),
                "click_count": sum(1 for r in campaign_rows if r["engagement"] == "clicked"),
                "active_lead_count": sum(1 for lead in leads if lead["campaign"] == name),
            }

        campaign_param = request.args.get("campaign")
        if campaign_param:
            if campaign_param not in campaigns:
                return jsonify({"error": f"unknown campaign {campaign_param!r}"}), 404
            return jsonify(_slice(campaign_param))
        return jsonify({"campaigns": [_slice(name) for name in campaigns]})

    @app.route("/api/reachouts")
    def api_reachouts():
        rows = dashboard.reachouts_rows(get_conn())
        # Campaign -> {light,dark} color map for row tinting (req 8.2), derived
        # live from the campaign names actually present in the rows — the React
        # table applies it as a CSS custom property, never a hardcoded hex.
        colors = {name: campaign_colors(name) for name in {r["campaign"] for r in rows if r["campaign"]}}
        return jsonify({"rows": rows, "total_count": len(rows), "campaign_colors": colors})

    @app.route("/api/logs")
    def api_logs():
        # Mirrors logs_page() exactly -- same LIMIT-not-pagination
        # convention, same event_display() enrichment per row.
        conn = get_conn()
        limit = request.args.get("limit", dashboard.EVENTS_DEFAULT_LIMIT, type=int)
        events = dashboard.recent_events(conn, limit=limit)
        for ev in events:
            ev["display"] = dashboard.event_display(ev)
        logs = {name: dashboard.read_log_tail(log_dir / name) for name in dashboard.LOG_FILES}
        return jsonify({
            "events": events,
            "total_count": len(events),
            "limit": limit,
            "truncated": len(events) == limit,
            "event_types": sorted(tracking.EVENT_TYPES),
            "logs": logs,
        })

    @app.route("/api/template-failures")
    def api_template_failures():
        failures = dashboard.template_failures()
        return jsonify({"failures": failures, "total_count": len(failures)})

    @app.route("/api/sync-status")
    def api_sync_status():
        # Lightweight banner poll -- reads the same cache
        # get_gmass_dependent_data() already maintains, never triggers a
        # second, independent GMass sweep of its own.
        gmass_data = dashboard.get_gmass_dependent_data(api_key, consumer_domains, redis_client, db_path)
        return jsonify({"sync_result": gmass_data["sync_result"], "cache_status": gmass_data["cache_status"]})

    @app.route("/api/followups")
    def api_followups():
        # Saved Remind templates (req 9) — the "list of previously-saved
        # follow-ups" the Remind popup shows.
        return jsonify({"followups": [
            {"slug": f["slug"], "title": f["title"]} for f in discover_followups()
        ]})

    @app.route("/api/reachouts/<string:recipient>/remind", methods=["POST"])
    def api_remind(recipient):
        # Queue a one-shot Remind (req 9). Body accepts either a saved-template
        # `slug`, or a raw `body` (optionally with `save_title` to save it as a
        # reusable template first — fail loud on a title clash, never a silent
        # overwrite). The send fires on the next drain (threaded reply).
        data = request.get_json(silent=True) or {}
        slug = data.get("slug")
        body = data.get("body")
        save_title = data.get("save_title")
        used_slug = slug
        try:
            if slug:
                body = load_followup(slug)["body"]
            elif body is not None:
                if save_title:
                    used_slug = save_followup(save_title, body)["slug"]
            else:
                return jsonify({"error": "provide a saved-followup 'slug' or a 'body'"}), 400
        except FollowupError as e:
            return jsonify({"error": str(e)}), 409
        try:
            dashboard.queue_remind_for(get_conn(), recipient, body, followup=used_slug)
        except (ValueError, QueueError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "followup": used_slug})

    @app.route("/api/commands")
    def api_commands():
        # Backs the dashboard's Commands reference tab (req 0) — derived from
        # the live argparse definition, never a hand-maintained list. Real
        # campaign names + a real recipient are threaded in so the per-command
        # examples read like the owner's own tool.
        conn = get_conn()
        row = conn.execute(
            "SELECT recipient FROM recipients ORDER BY last_event_at DESC LIMIT 1"
        ).fetchone()
        sample_recipient = row["recipient"] if row else None
        return jsonify({"commands": commands_reference(
            campaigns=list(discover_campaigns()), sample_recipient=sample_recipient)})

    @app.route("/api/nav")
    def api_nav():
        conn = get_conn()
        return jsonify({
            "template_failures_count": len(dashboard.template_failures()),
            "runner_staleness_warning": dashboard._runner_staleness_warning(conn, global_config.schedule),
        })

    # --- write endpoints -------------------------------------------------
    # Every route below mirrors an existing Jinja POST route's validation,
    # status codes, and error message text EXACTLY (same underlying
    # dashboard.py function call) -- only the input (JSON body instead of a
    # form) and the success/failure response shape (JSON instead of a
    # redirect) differ. See each mirrored route in dashboard.py for the full
    # reasoning behind the behavior being copied here.

    @app.route("/api/reply/<string:recipient>/tag", methods=["POST"])
    def api_reply_tag(recipient):
        body = request.get_json(silent=True) or {}
        tag = body.get("tag", "")
        resume_date_str = (body.get("resume_date") or "").strip()
        resume_date = None
        if resume_date_str:
            try:
                resume_date = date.fromisoformat(resume_date_str)
            except ValueError:
                return jsonify({"error": f"invalid resume_date {resume_date_str!r} — expected YYYY-MM-DD"}), 400
        try:
            dashboard.tag_reply(get_conn(), recipient, tag, resume_date=resume_date, api_key=api_key)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({
                "error": f"could not tag {recipient} ({tag!r}) — GMass suppression call failed, "
                         f"nothing was recorded: {e}"
            }), 502
        # Same cache-invalidation-on-success as reply_tag() -- any of the
        # four tags can change actionable_replies()'s output (see that
        # route's own comment for why this can't just sit stale for an hour).
        gmass_cache.invalidate(redis_client)
        return jsonify({"ok": True})

    @app.route("/api/reachouts/<string:recipient>/stop", methods=["POST"])
    def api_stop_outreach(recipient):
        try:
            dashboard.stop_outreach(get_conn(), recipient, api_key=api_key)
        except Exception as e:
            return jsonify({
                "error": f"could not stop outreach to {recipient} — GMass suppression call failed, "
                         f"nothing was recorded: {e}"
            }), 502
        return jsonify({"ok": True})

    @app.route("/api/reachouts/<string:recipient>/resend", methods=["POST"])
    def api_resend(recipient):
        body = request.get_json(silent=True) or {}
        corrected_email = (body.get("corrected_email") or "").strip()
        if not corrected_email:
            return jsonify({"error": "corrected_email is required"}), 400
        conn = get_conn()
        # Same dedup-check-for-display-only pattern as resend() -- purely
        # informational (warn, don't block); the owner already made an
        # explicit correction by submitting this request.
        dedup = check_recipient(conn, corrected_email, consumer_domains)
        warning = None
        if dedup.hard_warning:
            w = dedup.hard_warning
            warning = f"{corrected_email} already contacted — campaign={w.campaign} status={w.status}"
        elif dedup.soft_warning_contacts:
            warning = (f"{len(dedup.soft_warning_contacts)} other contact(s) already on domain "
                       f"{dedup.soft_warning_domain}")
        try:
            resend_bounced(conn, original_recipient=recipient, corrected_email=corrected_email,
                            archive_dir=archive.archive_dir_from_env())
        except QueueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "warning": warning})

    @app.route("/api/warm-but-silent/<string:recipient>/hide", methods=["POST"])
    def api_hide_warm_but_silent(recipient):
        ui_state.hide(get_conn(), recipient, dashboard.WARM_BUT_SILENT_WIDGET)
        return jsonify({"ok": True})

    @app.route("/api/warm-but-silent/<string:recipient>/unhide", methods=["POST"])
    def api_unhide_warm_but_silent(recipient):
        ui_state.unhide(get_conn(), recipient, dashboard.WARM_BUT_SILENT_WIDGET)
        return jsonify({"ok": True})

    @app.route("/api/reachouts/<string:recipient>/linkedin-replied", methods=["POST"])
    def api_linkedin_replied(recipient):
        # LinkedIn reply-gate (see dashboard.gate_linkedin): marking a recipient
        # replied-on-LinkedIn now HALTS their GMass outreach (status
        # 'linkedin-gate'), so this fires a real GMass unsubscribe first and
        # carries the same 502-on-failure, nothing-recorded contract as /stop.
        # One-way, like Stop: un-gating isn't supported (GMass unsubscribe is
        # account-wide, with no clean re-subscribe), so an explicit
        # {replied: false} fails loud rather than silently doing nothing.
        # Unknown recipient -> 404, same as any missing resource.
        body = request.get_json(silent=True) or {}
        if body.get("replied") is False:
            return jsonify({
                "error": "un-gating a LinkedIn-gated recipient is not supported (one-way, like Stop)"
            }), 400
        try:
            dashboard.gate_linkedin(get_conn(), recipient, api_key=api_key)
        except ValueError as e:
            return jsonify({"error": str(e)}), 404
        except Exception as e:
            return jsonify({
                "error": f"could not gate {recipient} via LinkedIn — GMass suppression call failed, "
                         f"nothing was recorded: {e}"
            }), 502
        return jsonify({"ok": True, "status": "linkedin-gate"})

    @app.route("/api/reachouts/<string:recipient>/followed-up", methods=["POST"])
    def api_followed_up(recipient):
        # "Mark followed up" (req 4): restarts the follow-up-reminder timer via
        # an interaction event. Each call is a fresh marker (no toggle).
        try:
            dashboard.mark_followed_up(get_conn(), recipient)
        except ValueError as e:
            return jsonify({"error": str(e)}), 404
        return jsonify({"ok": True})

    @app.route("/api/gmass/refresh", methods=["POST"])
    def api_gmass_refresh():
        # Mirrors gmass_refresh() -- manual escalation of
        # get_gmass_dependent_data's own background refresh. Reports
        # redis-unavailability in the body (200) rather than a redirect,
        # since there's no page to redirect a JSON caller back to.
        try:
            gmass_cache.ping(redis_client)
        except gmass_cache.RedisUnavailable:
            return jsonify({"ok": False, "reason": "redis_unavailable"})
        dashboard._spawn_background_refresh(db_path, api_key, consumer_domains, redis_client)
        return jsonify({"ok": True})
