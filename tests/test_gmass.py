"""GMass client tests (Build Order step 6) — fast, mocked, no network calls.

Response fixtures are grounded in the real captures in probes/findings/
(especially verify_20260702T205332Z.json) so the mocks match verified live
shapes, not invented ones. Genuine live-API verification of this exact
production code (not just mocks) lives in probes/run.py's 'client' probe,
run manually against a guarded self-send address — see CONTROL_SHEET.md.
"""
import html
from unittest.mock import MagicMock, patch

import pytest

from slap.gmass import (
    GMassError, build_campaign_settings, build_reply_settings, create_draft, get_reports, send_campaign,
    _plain_text_to_html,
)


def _response(status_code, body):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = str(body)
    return resp


# --- create_draft ------------------------------------------------------

@patch("slap.gmass.requests.post")
def test_create_draft_sends_expected_fields_and_header(mock_post):
    mock_post.return_value = _response(200, {"campaignDraftId": "r-123"})
    result = create_draft("key123", recipient="a@x.com", subject="Hi", message="Body")
    assert result == {"draft_id": "r-123", "raw": {"campaignDraftId": "r-123"}}

    url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
    assert url == "https://api.gmass.co/api/campaigndrafts"
    assert kwargs["headers"] == {"X-apikey": "key123"}
    # messageType must be "html" (not "text" -- not even a valid campaignDraft
    # enum value) for click tracking to have a real <a href> to rewrite; a
    # plain message with no HTML-special chars/URLs/newlines round-trips
    # through _plain_text_to_html unchanged.
    assert kwargs["json"] == {
        "emailAddresses": "a@x.com", "subject": "Hi", "message": "Body", "messageType": "html",
    }


# --- _plain_text_to_html -------------------------------------------------

def test_plain_text_to_html_linkifies_bare_urls_with_domain_as_display_text():
    # The actual root cause of a real BLOCKER: click tracking only rewrites
    # <a href> targets -- a bare URL string in a plain message has nothing
    # for GMass to rewrite, so clickTracking=true silently did nothing.
    # Verified live (three real guarded sends) that the display text must
    # differ from the href for GMass to rewrite it at all -- an <a href> whose
    # visible text is the SAME as the URL is left untouched. Domain-only text
    # is enough and needs no per-site label mapping.
    html_out = _plain_text_to_html("Link: https://www.linkedin.com/in/test")
    assert html_out == 'Link: <a href="https://www.linkedin.com/in/test">www.linkedin.com</a>'


def test_plain_text_to_html_escapes_special_characters():
    assert _plain_text_to_html("Tom & Jerry <3") == "Tom &amp; Jerry &lt;3"


def test_plain_text_to_html_preserves_line_breaks():
    assert _plain_text_to_html("Hi,\n\nBody line.\n") == "Hi,<br>\n<br>\nBody line.<br>\n"


def test_plain_text_to_html_handles_url_containing_ampersand():
    # & inside a URL must round-trip correctly through escape/unescape:
    # neither double-escaped (&amp;amp;) nor left as a raw & in HTML.
    url = "https://example.com/x?a=1&b=2"
    html_out = _plain_text_to_html(f"See {url}")
    assert html_out == 'See <a href="https://example.com/x?a=1&amp;b=2">example.com</a>'


def test_plain_text_to_html_multiple_urls_and_surrounding_text():
    html_out = _plain_text_to_html("A: https://a.com and B: https://b.com done")
    assert html_out == (
        'A: <a href="https://a.com">a.com</a> and '
        'B: <a href="https://b.com">b.com</a> done'
    )


@patch("slap.gmass.requests.post")
def test_create_draft_encodes_attachment_as_base64_json(mock_post):
    mock_post.return_value = _response(200, {"campaignDraftId": "r-123"})
    create_draft("key123", recipient="a@x.com", subject="Hi", message="Body",
                 attachment=("resume.pdf", b"%PDF-fake-bytes", "application/pdf"))

    sent_body = mock_post.call_args[1]["json"]
    assert sent_body["attachments"] == [{
        "fileName": "resume.pdf",
        "contentType": "application/pdf",
        "base64Content": "JVBERi1mYWtlLWJ5dGVz",
    }]


@patch("slap.gmass.requests.post")
def test_create_draft_falls_back_to_id_field(mock_post):
    mock_post.return_value = _response(200, {"id": "r-999"})
    result = create_draft("key", recipient="a@x.com", subject="s", message="m")
    assert result["draft_id"] == "r-999"


@patch("slap.gmass.requests.post")
def test_create_draft_http_error_raises_gmass_error(mock_post):
    mock_post.return_value = _response(400, {"error": "bad request"})
    with pytest.raises(GMassError, match="HTTP 400"):
        create_draft("key", recipient="a@x.com", subject="s", message="m")


@patch("slap.gmass.requests.post")
def test_create_draft_missing_id_raises(mock_post):
    mock_post.return_value = _response(200, {"unexpected": "shape"})
    with pytest.raises(GMassError, match="no draft id"):
        create_draft("key", recipient="a@x.com", subject="s", message="m")


# --- send_campaign -----------------------------------------------------

@patch("slap.gmass.requests.post")
def test_send_campaign_uses_path_form_not_body_form(mock_post):
    # Verified deviation from brief §3: draft id goes in the URL path; a
    # body-field {campaignDraftId: ...} form returns 400 on the live API.
    mock_post.return_value = _response(200, {"campaignId": 52126285})
    result = send_campaign("key", "r-123", campaign_settings={"openTracking": False})

    assert result == {"campaign_id": 52126285, "raw": {"campaignId": 52126285}}
    url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
    assert url == "https://api.gmass.co/api/campaigns/r-123"
    assert "campaignDraftId" not in kwargs["json"]
    assert kwargs["json"] == {"openTracking": False}


@patch("slap.gmass.requests.post")
def test_send_campaign_falls_back_to_id_field(mock_post):
    mock_post.return_value = _response(200, {"id": 42})
    result = send_campaign("key", "r-123", campaign_settings={})
    assert result["campaign_id"] == 42


@patch("slap.gmass.requests.post")
def test_send_campaign_http_error_raises(mock_post):
    mock_post.return_value = _response(400, {"error": "not saved yet"})
    with pytest.raises(GMassError, match="HTTP 400"):
        send_campaign("key", "r-123", campaign_settings={})


# --- get_reports ---------------------------------------------------------

@patch("slap.gmass.requests.get")
def test_get_reports_returns_data_list(mock_get):
    mock_get.return_value = _response(200, {
        "metadata": {"totalRecords": 1, "offset": 0, "limit": 100, "count": 1},
        "data": [{"emailAddress": "a@x.com", "gmailResponseText": "abc", "sentTime": "t", "sender": "s"}],
    })
    result = get_reports("key", 52126285, "recipients")
    assert result == [{"emailAddress": "a@x.com", "gmailResponseText": "abc", "sentTime": "t", "sender": "s"}]
    url = mock_get.call_args[0][0]
    assert url == "https://api.gmass.co/api/reports/52126285/recipients"


@patch("slap.gmass.requests.get")
def test_get_reports_empty_data_returns_empty_list(mock_get):
    mock_get.return_value = _response(200, {"metadata": {"totalRecords": 0}, "data": []})
    assert get_reports("key", 1, "replies") == []


@patch("slap.gmass.requests.get")
def test_get_reports_unknown_type_raises_before_network_call(mock_get):
    with pytest.raises(GMassError, match="unknown report type"):
        get_reports("key", 1, "bogus")
    mock_get.assert_not_called()


@patch("slap.gmass.requests.get")
def test_get_reports_http_error_raises(mock_get):
    mock_get.return_value = _response(500, {"error": "boom"})
    with pytest.raises(GMassError, match="HTTP 500"):
        get_reports("key", 1, "bounces")


# --- build_campaign_settings ----------------------------------------------

def test_build_campaign_settings_uses_english_ordinal_field_names():
    settings = build_campaign_settings([2, 3, 5], ["stage1 body", "stage2 body", "stage3 body"])
    assert settings["stageOneDays"] == 2
    assert settings["stageOneCampaignText"] == "stage1 body"
    assert settings["stageOneAction"] == "r"
    assert settings["stageTwoDays"] == 3
    assert settings["stageThreeDays"] == 5
    assert "stage1Days" not in settings
    assert "stage2Days" not in settings


def test_build_campaign_settings_defaults():
    settings = build_campaign_settings([2], ["body"])
    assert settings["openTracking"] is False
    assert settings["clickTracking"] is True
    assert settings["createDrafts"] is False


def test_build_campaign_settings_mismatched_lengths_raises():
    with pytest.raises(GMassError, match="stage bodies"):
        build_campaign_settings([2, 3, 5], ["only one body"])


def test_build_campaign_settings_too_many_stages_raises():
    with pytest.raises(GMassError, match="at most 8"):
        build_campaign_settings(list(range(9)), ["b"] * 9)


def test_build_campaign_settings_overrides():
    settings = build_campaign_settings([2], ["body"], open_tracking=True, create_drafts=True,
                                        stop_action="a")
    assert settings["openTracking"] is True
    assert settings["createDrafts"] is True
    assert settings["stageOneAction"] == "a"


# --- build_reply_settings (OOO resend, step 10) ---------------------------

def test_build_reply_settings_basic():
    settings = build_reply_settings(52120430)
    assert settings["sendAsReply"] is True
    assert settings["campaignIdToReplyTo"] == 52120430
    assert settings["openTracking"] is False
    assert settings["clickTracking"] is True
    assert settings["createDrafts"] is False


def test_build_reply_settings_coerces_campaign_id_to_int():
    # recipients.last_gmass_campaign_id is stored as TEXT — must coerce.
    settings = build_reply_settings("52120430")
    assert settings["campaignIdToReplyTo"] == 52120430
    assert isinstance(settings["campaignIdToReplyTo"], int)


def test_build_reply_settings_overrides():
    settings = build_reply_settings(1, open_tracking=True, create_drafts=True)
    assert settings["openTracking"] is True
    assert settings["createDrafts"] is True
