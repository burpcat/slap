"""Template reload tests (post-launch feature, slap.reload) — see that
module's own docstring for the two things confirmed against the real
send-call code before this was built (stage content is locked into GMass at
initial send; raw field values weren't persisted anywhere until this
feature added them):

- clean no-op when nothing changed
- re-render reflects an edited template
- a recipient with a real send already fired (first_sent_at set) is
  completely excluded/untouched by any later template edit
- a template edited to reference a field one recipient's stored values lack
  fails ONLY that recipient; an unrelated recipient in the same run/campaign
  still succeeds
- a recipient staged before field_values existed (None) fails with its own
  distinct, actionable reason
- apply_changes() only ever touches subject/body/stage_bodies
- apply_changes() isolates a per-recipient write failure from the rest of
  the batch, and reports it rather than raising
- a recipient with an OPEN draft (create_draft succeeded, send_campaign
  didn't) is excluded just like an already-sent one -- its initial
  subject/body are already locked into that draft (iron-audit SHOULD-FIX)
- write_failures()/load_failures() round-trip and fully overwrite (no
  merging with a prior run — see module docstring for why that's what makes
  "resolved" need no separate bookkeeping)
"""
import shutil

from slap.config import GlobalConfig, ScheduleConfig
from slap.latex import recipient_workdir as _recipient_workdir
from slap.queue import load_manifest, stage_recipient
from slap.reload import apply_changes, load_failures, scan, write_failures
from slap.tracking import append_event, connect


def make_global_config(tmp_path, *, signature=""):
    return GlobalConfig(
        from_email="owner@gmail.com", from_name="Owner", api_key_env="GMASS_API_KEY",
        personas={"recruiter": [2, 3, 5]},
        schedule=ScheduleConfig(
            fire_window_start="09:00", fire_window_end="09:15",
            send_delay_min=10, send_delay_max=15, daily_cap=500, drain_retries=3,
            active_days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        ),
        consumer_domains_file=str(tmp_path / "consumer_domains.txt"), path=tmp_path / "config.yaml",
        signature=signature,
    )


DEFAULT_FIELDS_YAML = (
    "  - { key: email,        label: Email }\n"
    "  - { key: company,      label: Company }\n"
    "  - { key: founder_name, label: Founder name }\n"
)


def write_campaign(tmp_path, name="coldpost", *, initial_txt=None, stage_bodies=None,
                    fields_yaml=DEFAULT_FIELDS_YAML):
    campaigns_dir = tmp_path / "campaigns"
    campaign_dir = campaigns_dir / name
    campaign_dir.mkdir(parents=True, exist_ok=True)
    (campaign_dir / "campaign.yaml").write_text(
        "persona: recruiter\n"
        "latex:\n"
        "  enabled: false\n"
        "  attachment_name: resume.pdf\n"
        "attachment_file: resume.pdf\n"
        "fields:\n" + fields_yaml
    )
    (campaign_dir / "resume.pdf").write_bytes(b"%PDF-fake")
    (campaign_dir / "initial.txt").write_text(
        initial_txt or "Subject: Hi {{company}}\n\nHello from {{founder_name}}.\n"
    )
    stage_bodies = stage_bodies or ["Following up 1", "Following up 2", "Following up 3"]
    for i, body in enumerate(stage_bodies, start=1):
        (campaign_dir / f"stage{i}.txt").write_text(body + "\n")
    return campaigns_dir


def stage_one(conn, tmp_path, *, recipient="jane@acme.com", campaign="coldpost", field_values=None,
              subject="Hi Acme", body="Hello from Founder.", stage_bodies=None, workdir_root=None):
    workdir_root = workdir_root or (tmp_path / "workdir")
    attachment = tmp_path / "resume.pdf"
    if not attachment.exists():
        attachment.write_bytes(b"%PDF-fake")
    return stage_recipient(
        conn, campaign=campaign, recipient=recipient, persona="recruiter", cadence=[2, 3, 5],
        subject=subject, body=body,
        stage_bodies=stage_bodies or ["Following up 1", "Following up 2", "Following up 3"],
        attachment_path=attachment, attachment_name="resume.pdf", latex_enabled=False,
        workdir_root=workdir_root, field_values=field_values,
    )


def test_scan_no_template_changes_is_clean_noop(tmp_path):
    conn = connect(tmp_path / "test.db")
    campaigns_dir = write_campaign(tmp_path)
    stage_one(conn, tmp_path, field_values={"email": "jane@acme.com", "company": "Acme", "founder_name": "Founder"})

    plan = scan(conn, make_global_config(tmp_path), workdir_root=tmp_path / "workdir", campaigns_dir=campaigns_dir)

    assert plan.changed == []
    assert plan.failures == []
    assert plan.unchanged == [("jane@acme.com", "coldpost")]


def test_scan_reflects_edited_template(tmp_path):
    conn = connect(tmp_path / "test.db")
    campaigns_dir = write_campaign(tmp_path)
    stage_one(conn, tmp_path, field_values={"email": "jane@acme.com", "company": "Acme", "founder_name": "Founder"})

    # Owner edits the body template after staging.
    (campaigns_dir / "coldpost" / "initial.txt").write_text(
        "Subject: Hi {{company}}\n\nHello there, {{founder_name}}!\n"
    )

    plan = scan(conn, make_global_config(tmp_path), workdir_root=tmp_path / "workdir", campaigns_dir=campaigns_dir)

    assert plan.failures == []
    assert plan.unchanged == []
    assert len(plan.changed) == 1
    change = plan.changed[0]
    assert change.recipient == "jane@acme.com"
    assert change.campaign == "coldpost"
    assert change.old_body == "Hello from Founder."
    assert change.new_body == "Hello there, Founder!"
    assert change.new_subject == "Hi Acme"  # unedited part of the template is unaffected


def test_apply_changes_only_touches_subject_body_stage_bodies(tmp_path):
    conn = connect(tmp_path / "test.db")
    campaigns_dir = write_campaign(tmp_path)
    workdir_root = tmp_path / "workdir"
    workdir = stage_one(conn, tmp_path, workdir_root=workdir_root,
                         field_values={"email": "jane@acme.com", "company": "Acme", "founder_name": "Founder"})
    before = load_manifest(workdir)

    (campaigns_dir / "coldpost" / "initial.txt").write_text(
        "Subject: Hi {{company}}\n\nHello there, {{founder_name}}!\n"
    )
    plan = scan(conn, make_global_config(tmp_path), workdir_root=workdir_root, campaigns_dir=campaigns_dir)
    apply_changes(plan.changed, workdir_root=workdir_root)

    after = load_manifest(workdir)
    assert after["body"] == "Hello there, Founder!"
    for key in ("campaign", "recipient", "persona", "cadence", "attachment_name", "attachment_source",
                "field_values"):
        assert after[key] == before[key]


def test_scan_excludes_recipient_already_sent_and_leaves_them_untouched(tmp_path):
    conn = connect(tmp_path / "test.db")
    campaigns_dir = write_campaign(tmp_path)
    workdir_root = tmp_path / "workdir"
    workdir = stage_one(conn, tmp_path, workdir_root=workdir_root,
                         field_values={"email": "jane@acme.com", "company": "Acme", "founder_name": "Founder"})
    before = load_manifest(workdir)

    append_event(conn, type="sent", recipient="jane@acme.com", campaign="coldpost", stage=0,
                 gmass_campaign_id="c1", gmass_draft_id="d1", meta={"is_final_stage": False})

    # Even a template edit that WOULD change their content if they were still
    # queued must have zero effect once a real send has fired.
    (campaigns_dir / "coldpost" / "initial.txt").write_text(
        "Subject: Hi {{company}}\n\nHello there, {{founder_name}}!\n"
    )
    plan = scan(conn, make_global_config(tmp_path), workdir_root=workdir_root, campaigns_dir=campaigns_dir)

    assert plan.changed == []
    assert plan.unchanged == []
    assert plan.failures == []
    assert load_manifest(workdir) == before


def test_scan_missing_placeholder_fails_only_that_recipient(tmp_path):
    conn = connect(tmp_path / "test.db")
    campaigns_dir = write_campaign(tmp_path)
    workdir_root = tmp_path / "workdir"

    # A: staged with special_note already in their stored values (as if
    # staged after the field existed).
    stage_one(conn, tmp_path, recipient="a@acme.com", workdir_root=workdir_root,
              field_values={"email": "a@acme.com", "company": "Acme", "founder_name": "Founder",
                            "special_note": "custom note"},
              stage_bodies=["Following up 1", "Following up 2", "Following up 3"])
    # B: staged before special_note existed — key genuinely absent.
    stage_one(conn, tmp_path, recipient="b@acme.com", workdir_root=workdir_root,
              field_values={"email": "b@acme.com", "company": "Acme", "founder_name": "Founder"},
              stage_bodies=["Following up 1", "Following up 2", "Following up 3"])

    # Owner adds a new field + references it in stage1.txt.
    (campaigns_dir / "coldpost" / "campaign.yaml").write_text(
        "persona: recruiter\n"
        "latex:\n"
        "  enabled: false\n"
        "  attachment_name: resume.pdf\n"
        "attachment_file: resume.pdf\n"
        "fields:\n" + DEFAULT_FIELDS_YAML + "  - { key: special_note, label: Special note, optional: true }\n"
    )
    (campaigns_dir / "coldpost" / "stage1.txt").write_text("Following up 1: {{special_note}}\n")

    plan = scan(conn, make_global_config(tmp_path), workdir_root=workdir_root, campaigns_dir=campaigns_dir)

    assert len(plan.changed) == 1
    assert plan.changed[0].recipient == "a@acme.com"
    assert plan.changed[0].new_stage_bodies[0] == "Following up 1: custom note"

    assert len(plan.failures) == 1
    failure = plan.failures[0]
    assert failure.recipient == "b@acme.com"
    assert failure.missing_fields == ["special_note"]
    assert "special_note" in failure.reason
    assert failure.attempted_at


def test_scan_no_stored_field_values_fails_with_distinct_reason(tmp_path):
    conn = connect(tmp_path / "test.db")
    campaigns_dir = write_campaign(tmp_path)
    workdir_root = tmp_path / "workdir"
    # field_values=None -- simulates a recipient staged before this feature.
    stage_one(conn, tmp_path, workdir_root=workdir_root, field_values=None)

    plan = scan(conn, make_global_config(tmp_path), workdir_root=workdir_root, campaigns_dir=campaigns_dir)

    assert plan.changed == []
    assert plan.unchanged == []
    assert len(plan.failures) == 1
    assert plan.failures[0].missing_fields == []
    assert "before template-reload existed" in plan.failures[0].reason


def test_scan_invalid_campaign_config_fails_all_its_recipients_only(tmp_path):
    conn = connect(tmp_path / "test.db")
    campaigns_dir = write_campaign(tmp_path, name="broken")
    write_campaign(tmp_path, name="healthy")
    workdir_root = tmp_path / "workdir"

    stage_one(conn, tmp_path, recipient="a@x.com", campaign="broken", workdir_root=workdir_root,
              field_values={"email": "a@x.com", "company": "X", "founder_name": "F"})
    stage_one(conn, tmp_path, recipient="b@y.com", campaign="healthy", workdir_root=workdir_root,
              field_values={"email": "b@y.com", "company": "Acme", "founder_name": "Founder"})

    # Break the "broken" campaign's config after staging.
    (campaigns_dir / "broken" / "campaign.yaml").write_text("persona: nonexistent-persona\n")

    plan = scan(conn, make_global_config(tmp_path), workdir_root=workdir_root, campaigns_dir=campaigns_dir)

    assert len(plan.failures) == 1
    assert plan.failures[0].recipient == "a@x.com"
    assert "campaign config is currently invalid" in plan.failures[0].reason
    assert plan.unchanged == [("b@y.com", "healthy")]


def test_write_and_load_failures_round_trip_and_fully_overwrite(tmp_path):
    path = tmp_path / "template_reload_failures.json"
    conn = connect(tmp_path / "test.db")
    campaigns_dir = write_campaign(tmp_path)
    workdir_root = tmp_path / "workdir"
    stage_one(conn, tmp_path, field_values=None)  # guaranteed failure

    plan = scan(conn, make_global_config(tmp_path), workdir_root=workdir_root, campaigns_dir=campaigns_dir)
    write_failures(plan.failures, path=path)
    assert len(load_failures(path=path)) == 1

    # A later run that finds nothing wrong must fully clear the file, not
    # merge with the earlier failure -- this is what "resolved" means.
    write_failures([], path=path)
    assert load_failures(path=path) == []


def test_load_failures_missing_file_is_empty_not_an_error(tmp_path):
    assert load_failures(path=tmp_path / "does-not-exist.json") == []


def test_scan_excludes_recipient_with_open_draft_and_leaves_them_untouched(tmp_path):
    # create_draft succeeded (real draft_created event) but send_campaign
    # never fired (no `sent` event) -- due_recipients() still calls this
    # "queued," but the initial subject/body are already locked into that
    # open GMass draft (iron-audit SHOULD-FIX: reloading here would silently
    # split-brain the initial send against the follow-up stages).
    conn = connect(tmp_path / "test.db")
    campaigns_dir = write_campaign(tmp_path)
    workdir_root = tmp_path / "workdir"
    workdir = stage_one(conn, tmp_path, workdir_root=workdir_root,
                         field_values={"email": "jane@acme.com", "company": "Acme", "founder_name": "Founder"})
    before = load_manifest(workdir)

    append_event(conn, type="draft_created", recipient="jane@acme.com", campaign="coldpost", stage=0,
                 gmass_draft_id="draft-1")

    (campaigns_dir / "coldpost" / "initial.txt").write_text(
        "Subject: Hi {{company}}\n\nHello there, {{founder_name}}!\n"
    )
    plan = scan(conn, make_global_config(tmp_path), workdir_root=workdir_root, campaigns_dir=campaigns_dir)

    assert plan.changed == []
    assert plan.unchanged == []
    assert len(plan.failures) == 1
    assert plan.failures[0].recipient == "jane@acme.com"
    assert "open GMass draft" in plan.failures[0].reason
    assert load_manifest(workdir) == before


def test_apply_changes_isolates_a_per_recipient_write_failure(tmp_path):
    conn = connect(tmp_path / "test.db")
    campaigns_dir = write_campaign(tmp_path)
    workdir_root = tmp_path / "workdir"
    stage_one(conn, tmp_path, recipient="a@acme.com", workdir_root=workdir_root,
              field_values={"email": "a@acme.com", "company": "Acme", "founder_name": "Founder"})
    workdir_b = stage_one(conn, tmp_path, recipient="b@acme.com", workdir_root=workdir_root,
                           field_values={"email": "b@acme.com", "company": "Acme", "founder_name": "Founder"})

    (campaigns_dir / "coldpost" / "initial.txt").write_text(
        "Subject: Hi {{company}}\n\nHello there, {{founder_name}}!\n"
    )
    plan = scan(conn, make_global_config(tmp_path), workdir_root=workdir_root, campaigns_dir=campaigns_dir)
    assert len(plan.changed) == 2

    # Something external deletes b's workdir between scan() and apply_changes().
    shutil.rmtree(workdir_b)

    apply_failures = apply_changes(plan.changed, workdir_root=workdir_root)

    assert len(apply_failures) == 1
    assert apply_failures[0].recipient == "b@acme.com"
    # a@acme.com's write must still have gone through despite b's failure.
    a_change = next(c for c in plan.changed if c.recipient == "a@acme.com")
    a_manifest = load_manifest(_recipient_workdir("coldpost", "a@acme.com", root=workdir_root))
    assert a_manifest["body"] == a_change.new_body
