"""Domain/recipient dedup tests (Build Order step 7), per SLAP_BUILD_PROMPT.md
§13 B: exact-recipient hard warn; non-consumer soft warn; gmail correctly
skipped for soft warn; warning shows prior-contact context.
"""
import pytest

from slap.domains import DomainsError, check_recipient, domain_index, domain_of, load_consumer_domains
from slap.tracking import append_event, connect

CONSUMER_DOMAINS = {"gmail.com", "outlook.com", "yahoo.com"}


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "test.db")


# --- load_consumer_domains ---------------------------------------------

def test_load_consumer_domains_reads_seed_file(tmp_path):
    path = tmp_path / "consumer_domains.txt"
    path.write_text("gmail.com\nOUTLOOK.com\n\nyahoo.com\n")
    assert load_consumer_domains(path) == {"gmail.com", "outlook.com", "yahoo.com"}


def test_load_consumer_domains_missing_file_fails_loud(tmp_path):
    with pytest.raises(DomainsError, match="not found"):
        load_consumer_domains(tmp_path / "consumer_domains.txt")


def test_load_consumer_domains_default_file_has_expected_seed():
    # The real committed repo-root file from Build Order step 2.
    domains = load_consumer_domains()
    assert {"gmail.com", "outlook.com", "yahoo.com", "icloud.com", "protonmail.com"} <= domains


# --- domain_of -----------------------------------------------------------

def test_domain_of_extracts_and_lowercases():
    assert domain_of("Jane@ACME.com") == "acme.com"


def test_domain_of_no_at_sign_fails_loud():
    with pytest.raises(DomainsError, match="single-@"):
        domain_of("not-an-email")


def test_domain_of_multiple_at_signs_fails_loud():
    with pytest.raises(DomainsError, match="single-@"):
        domain_of("a@b@c.com")


# --- check_recipient: hard warn -------------------------------------------

def test_check_recipient_no_warn_for_brand_new_recipient(conn):
    result = check_recipient(conn, "new@acme.com", CONSUMER_DOMAINS)
    assert result.hard_warning is None
    assert result.soft_warning_contacts is None


def test_check_recipient_hard_warn_on_exact_match(conn):
    append_event(conn, type="queued", recipient="jane@acme.com", campaign="c1",
                 stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="jane@acme.com", campaign="c1", stage=0)

    result = check_recipient(conn, "jane@acme.com", CONSUMER_DOMAINS)
    assert result.hard_warning is not None
    assert result.hard_warning.recipient == "jane@acme.com"
    assert result.hard_warning.campaign == "c1"


def test_check_recipient_hard_warn_is_case_insensitive(conn):
    append_event(conn, type="queued", recipient="jane@acme.com", campaign="c1", stage=0)
    result = check_recipient(conn, "Jane@Acme.com", CONSUMER_DOMAINS)
    assert result.hard_warning is not None


def test_check_recipient_hard_warn_fires_even_on_consumer_domain(conn):
    # The brief's own test note: a repeated +testmassN@gmail.com must still
    # hard-warn even though gmail.com is a consumer domain.
    append_event(conn, type="queued", recipient="everythingforgenius+testmass1@gmail.com",
                 campaign="c1", stage=0)
    result = check_recipient(conn, "everythingforgenius+testmass1@gmail.com", CONSUMER_DOMAINS)
    assert result.hard_warning is not None


# --- check_recipient: soft warn --------------------------------------------

def test_check_recipient_soft_warn_on_same_non_consumer_domain(conn):
    append_event(conn, type="queued", recipient="jane@acme.com", campaign="c1", stage=0)
    result = check_recipient(conn, "john@acme.com", CONSUMER_DOMAINS)
    assert result.hard_warning is None
    assert result.soft_warning_domain == "acme.com"
    assert [c.recipient for c in result.soft_warning_contacts] == ["jane@acme.com"]


def test_check_recipient_consumer_domain_skips_soft_warn(conn):
    # The brief's own test note: the owner's test addresses are all
    # @gmail.com (a consumer domain) — soft warn must correctly SKIP them
    # even though a different +testmassN was contacted before.
    append_event(conn, type="queued", recipient="everythingforgenius+testmass1@gmail.com",
                 campaign="c1", stage=0)
    result = check_recipient(conn, "everythingforgenius+testmass2@gmail.com", CONSUMER_DOMAINS)
    assert result.soft_warning_domain is None
    assert result.soft_warning_contacts is None
    # And the hard warn correctly does NOT fire either, since it's a different recipient.
    assert result.hard_warning is None


def test_check_recipient_soft_warn_excludes_the_exact_match_itself(conn):
    # A recipient contacted before on a non-consumer domain shouldn't show up
    # in their OWN soft-warning contact list when re-checked (that's the hard
    # warn's job, not the "different person" soft warn's).
    append_event(conn, type="queued", recipient="jane@acme.com", campaign="c1", stage=0)
    result = check_recipient(conn, "jane@acme.com", CONSUMER_DOMAINS)
    assert result.soft_warning_contacts is None


def test_check_recipient_warning_context_reflects_replied_state(conn):
    append_event(conn, type="queued", recipient="jane@acme.com", campaign="c1", stage=0)
    append_event(conn, type="reply", recipient="jane@acme.com", campaign="c1")

    result = check_recipient(conn, "jane@acme.com", CONSUMER_DOMAINS)
    assert result.hard_warning.status == "replied"
    assert result.hard_warning.replied_at is not None


# --- domain_index / the `domains` command's data source --------------------

def test_domain_index_empty_when_no_contacts(conn):
    assert domain_index(conn) == {}


def test_domain_index_groups_by_domain(conn):
    append_event(conn, type="queued", recipient="jane@acme.com", campaign="c1", stage=0)
    append_event(conn, type="queued", recipient="john@acme.com", campaign="c1", stage=0)
    append_event(conn, type="queued", recipient="sam@other.com", campaign="c1", stage=0)

    index = domain_index(conn)
    assert set(index.keys()) == {"acme.com", "other.com"}
    assert {c.recipient for c in index["acme.com"]} == {"jane@acme.com", "john@acme.com"}
    assert {c.recipient for c in index["other.com"]} == {"sam@other.com"}
