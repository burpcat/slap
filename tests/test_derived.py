from pathlib import Path

from slap import derived
from slap.config import GlobalConfig, ScheduleConfig


def _cfg(tmp_path):
    (tmp_path / "docs").mkdir()
    for name in ("ai.pdf", "ds.pdf"):
        (tmp_path / "docs" / name).write_bytes(b"%PDF " + name.encode())
    return GlobalConfig(from_email="a@x.com", from_name="A", api_key_env="K", personas={"r": [1]},
                        schedule=ScheduleConfig("09:00", "09:15", 0, 0, 1, 0, ["mon"]),
                        consumer_domains_file="c.txt", path=Path("config.yaml"),
                        docs_dir=tmp_path / "docs", send_tags={"ai": "ai.pdf", "ds": "ds.pdf"})


def test_cards_stale_until_saved_and_after_pdf_edit(tmp_path):
    cfg = _cfg(tmp_path)
    assert [c["stale"] for c in derived.resume_cards(cfg)] == [True, True]
    derived.save_derived(cfg, "card:ai", cfg.docs_dir / "ai.pdf", "LLM agents RAG")
    assert derived.resume_cards(cfg)[0] == {"tag": "ai", "file": "ai.pdf", "card": "LLM agents RAG", "stale": False}
    (cfg.docs_dir / "ai.pdf").write_bytes(b"%PDF edited")
    assert derived.resume_cards(cfg)[0]["stale"] is True


def test_suggest_resume_picks_best_overlap_or_none():
    cards = {"ai": "LLM agents, RAG pipelines, evals, PyTorch", "ds": "A/B testing, causal inference, SQL, dashboards"}
    jd = "We build LLM agents with RAG and rigorous evals."
    assert derived.suggest_resume(jd, cards)["tag"] == "ai"
    assert derived.suggest_resume("Forklift operator, night shift", cards) is None
    assert derived.suggest_resume(jd, {}) is None
