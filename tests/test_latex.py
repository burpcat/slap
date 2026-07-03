"""LaTeX loop tests (Build Order step 8), per SLAP_BUILD_PROMPT.md §13 B:
compiles a sample; >1-page triggers the hard gate; abort cleans workdir;
rename-to-attachment_name.

Fast tests use an injected fake compile_fn (no real xelatex/GUI apps) and
run automatically. Tests marked `slow` actually invoke xelatex and are
excluded from the default run (see pytest.ini) — run with `pytest -m slow`.
"""
import shutil

import pytest

from slap.latex import (
    CompileResult, LatexError, abort_workdir, compile_tex, page_count,
    recipient_workdir, run_latex_loop, stage_final_pdf, tex_hash, write_tex,
)

XELATEX_AVAILABLE = shutil.which("xelatex") is not None


class ScriptedInput:
    """Feeds a fixed sequence of commands to read_command, like a scripted
    terminal session."""
    def __init__(self, commands):
        self.commands = list(commands)

    def __call__(self, prompt=""):
        return self.commands.pop(0)


def fake_compile(success=True, pages=1, log=""):
    def _compile(tex_path):
        pdf_path = tex_path.with_suffix(".pdf") if success else None
        if success:
            pdf_path.write_bytes(b"%PDF-fake")
        return CompileResult(success=success, pdf_path=pdf_path, pages=pages if success else None, log=log)
    return _compile


def noop(*a, **k):
    pass


# --- recipient_workdir / write_tex / tex_hash -----------------------------

def test_recipient_workdir_creates_isolated_per_recipient_dirs(tmp_path):
    d1 = recipient_workdir("campaign-a", "jane@x.com", root=tmp_path)
    d2 = recipient_workdir("campaign-a", "john@x.com", root=tmp_path)
    assert d1 != d2
    assert d1.exists() and d2.exists()
    assert d1.parent == d2.parent  # same campaign


def test_write_tex_writes_resume_tex(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    tex_path = write_tex(workdir, "\\documentclass{article}")
    assert tex_path.name == "resume.tex"
    assert tex_path.read_text() == "\\documentclass{article}"


def test_tex_hash_changes_when_content_changes(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    p1 = write_tex(workdir, "version one")
    h1 = tex_hash(p1)
    p2 = write_tex(workdir, "version two")
    h2 = tex_hash(p2)
    assert h1 != h2


def test_abort_workdir_removes_directory(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    write_tex(workdir, "x")
    assert workdir.exists()
    abort_workdir(workdir)
    assert not workdir.exists()


def test_stage_final_pdf_renames_and_writes_hash_sidecar(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    tex_path = write_tex(workdir, "content")
    pdf_path = workdir / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    staged = stage_final_pdf(pdf_path, tex_path, "Jane_Resume.pdf")
    assert staged.path == workdir / "Jane_Resume.pdf"
    assert staged.path.exists()
    assert not pdf_path.exists()  # renamed, not copied
    assert (workdir / "Jane_Resume.pdf.hash").read_text() == staged.tex_hash
    assert staged.tex_hash == tex_hash(tex_path)


# --- run_latex_loop: fast, mocked ------------------------------------------

def test_loop_done_with_one_page_stages_immediately(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    result = run_latex_loop(
        workdir, "tex source", "Jane_Resume.pdf",
        read_command=ScriptedInput(["d"]), compile_fn=fake_compile(pages=1),
        open_editor=noop, open_preview=noop,
    )
    assert result is not None
    assert result.path == workdir / "Jane_Resume.pdf"
    assert result.path.exists()


def test_loop_abort_cleans_workdir_and_returns_none(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    result = run_latex_loop(
        workdir, "tex source", "Jane_Resume.pdf",
        read_command=ScriptedInput(["a"]), compile_fn=fake_compile(pages=1),
        open_editor=noop, open_preview=noop,
    )
    assert result is None
    assert not workdir.exists()


def test_loop_hard_gate_blocks_until_exact_phrase(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    scripted = ScriptedInput(["d", "y", "send 2 pages anyway"])
    result = run_latex_loop(
        workdir, "tex source", "Jane_Resume.pdf",
        read_command=scripted, compile_fn=fake_compile(pages=2), open_editor=noop, open_preview=noop,
    )
    assert result is not None
    assert result.path.exists()
    # Every scripted command was actually consumed by the gate re-prompting
    # past "y" — proving "y" did NOT short-circuit it (a regression making
    # "y" an accepted shortcut would leave "send 2 pages anyway" unconsumed
    # while still returning non-None here).
    assert scripted.commands == []


def test_loop_hard_gate_rejects_bare_yes(tmp_path):
    # If "y" incorrectly became an accepted shortcut, this returns a
    # StagedAttachment instead of raising — proving the gate genuinely
    # requires the exact phrase, not merely that the phrase also works.
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    with pytest.raises(IndexError):
        run_latex_loop(
            workdir, "tex source", "Jane_Resume.pdf",
            read_command=ScriptedInput(["d", "y"]), compile_fn=fake_compile(pages=2),
            open_editor=noop, open_preview=noop,
        )


def test_loop_hard_gate_r_returns_to_main_loop_without_staging(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    result = run_latex_loop(
        workdir, "tex source", "Jane_Resume.pdf",
        read_command=ScriptedInput(["d", "r", "d", "send 2 pages anyway"]),
        compile_fn=fake_compile(pages=2), open_editor=noop, open_preview=noop,
    )
    # First 'd' hits the gate; 'r' declines and loops back to the main
    # [r/o/d/a] prompt (not the gate's own prompt again); second 'd' hits the
    # gate again; this time the exact phrase confirms.
    assert result is not None


def test_loop_recompile_command_invokes_compile_fn_again(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    calls = []

    def counting_compile(tex_path):
        calls.append(1)
        return fake_compile(pages=1)(tex_path)

    run_latex_loop(
        workdir, "tex source", "Jane_Resume.pdf",
        read_command=ScriptedInput(["r", "d"]), compile_fn=counting_compile,
        open_editor=noop, open_preview=noop,
    )
    # One compile on initial paste, one on 'r', one authoritative on 'd'.
    assert len(calls) == 3


def test_loop_open_editor_command_invokes_open_editor(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    calls = []
    run_latex_loop(
        workdir, "tex source", "Jane_Resume.pdf",
        read_command=ScriptedInput(["o", "d"]), compile_fn=fake_compile(pages=1),
        open_editor=lambda p: calls.append(p), open_preview=noop,
    )
    assert len(calls) == 2  # once on initial paste, once on 'o'


def test_loop_failed_compile_on_done_loops_back_instead_of_staging(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    result = run_latex_loop(
        workdir, "tex source", "Jane_Resume.pdf",
        read_command=ScriptedInput(["d", "a"]),
        compile_fn=fake_compile(success=False, log="! Undefined control sequence."),
        open_editor=noop, open_preview=noop,
    )
    assert result is None  # never staged; loop continued to 'a' and aborted


def test_loop_unknown_command_reprompts(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    result = run_latex_loop(
        workdir, "tex source", "Jane_Resume.pdf",
        read_command=ScriptedInput(["bogus", "d"]), compile_fn=fake_compile(pages=1),
        open_editor=noop, open_preview=noop,
    )
    assert result is not None


def _eof_after(commands):
    remaining = list(commands)

    def _read(prompt=""):
        if not remaining:
            raise EOFError()
        return remaining.pop(0)
    return _read


def test_loop_eof_at_main_prompt_aborts_cleanly(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    result = run_latex_loop(
        workdir, "tex source", "Jane_Resume.pdf",
        read_command=_eof_after([]), compile_fn=fake_compile(pages=1),
        open_editor=noop, open_preview=noop,
    )
    assert result is None
    assert not workdir.exists()  # cleaned up like a normal abort, not left half-done


def test_loop_eof_during_gate_aborts_not_confirms(tmp_path):
    # EOF arriving mid-gate (e.g. piped/closed stdin right after 'd') must
    # never be treated as the confirmation — fail closed, with cleanup.
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    result = run_latex_loop(
        workdir, "tex source", "Jane_Resume.pdf",
        read_command=_eof_after(["d"]), compile_fn=fake_compile(pages=2),
        open_editor=noop, open_preview=noop,
    )
    assert result is None
    assert not workdir.exists()


def test_loop_keyboard_interrupt_aborts_cleanly(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)

    def interrupt(prompt=""):
        raise KeyboardInterrupt()

    result = run_latex_loop(
        workdir, "tex source", "Jane_Resume.pdf",
        read_command=interrupt, compile_fn=fake_compile(pages=1),
        open_editor=noop, open_preview=noop,
    )
    assert result is None
    assert not workdir.exists()


# --- real xelatex compilation (slow, gated) --------------------------------

ONE_PAGE_TEX = r"""
\documentclass{article}
\begin{document}
Hello, one page.
\end{document}
"""

TWO_PAGE_TEX = r"""
\documentclass{article}
\begin{document}
Page one.
\newpage
Page two.
\end{document}
"""

BROKEN_TEX = r"""
\documentclass{article}
\begin{document}
\undefinedcommandthatdoesnotexist
\end{document}
"""


@pytest.mark.slow
@pytest.mark.skipif(not XELATEX_AVAILABLE, reason="xelatex not installed")
def test_real_compile_one_page_document(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    tex_path = write_tex(workdir, ONE_PAGE_TEX)
    result = compile_tex(tex_path)
    assert result.success is True
    assert result.pages == 1
    assert result.pdf_path.exists()


@pytest.mark.slow
@pytest.mark.skipif(not XELATEX_AVAILABLE, reason="xelatex not installed")
def test_real_compile_two_page_document_triggers_gate_condition(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    tex_path = write_tex(workdir, TWO_PAGE_TEX)
    result = compile_tex(tex_path)
    assert result.success is True
    assert result.pages == 2


@pytest.mark.slow
@pytest.mark.skipif(not XELATEX_AVAILABLE, reason="xelatex not installed")
def test_real_compile_broken_document_fails_with_no_pdf(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    tex_path = write_tex(workdir, BROKEN_TEX)
    result = compile_tex(tex_path)
    assert result.success is False
    assert result.pdf_path is None
    assert result.pages is None
    assert "!" in result.log  # xelatex error marker


@pytest.mark.slow
@pytest.mark.skipif(not XELATEX_AVAILABLE, reason="xelatex not installed")
def test_real_compile_deletes_stale_pdf_before_recompiling(tmp_path):
    # The core anti-stale-PDF guarantee: a successful compile followed by a
    # broken recompile must NOT leave the old good PDF looking like success.
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    tex_path = write_tex(workdir, ONE_PAGE_TEX)
    good = compile_tex(tex_path)
    assert good.success is True
    pdf_path = good.pdf_path

    write_tex(workdir, BROKEN_TEX)
    bad = compile_tex(tex_path)
    assert bad.success is False
    assert bad.pdf_path is None
    assert not pdf_path.exists()  # the stale good PDF was actually removed


@pytest.mark.slow
@pytest.mark.skipif(not XELATEX_AVAILABLE, reason="xelatex not installed")
def test_real_page_count_matches_compile_result(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    tex_path = write_tex(workdir, TWO_PAGE_TEX)
    result = compile_tex(tex_path)
    assert page_count(result.pdf_path) == 2


@pytest.mark.slow
@pytest.mark.skipif(not XELATEX_AVAILABLE, reason="xelatex not installed")
def test_real_full_loop_one_page_stages_correctly(tmp_path):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    result = run_latex_loop(
        workdir, ONE_PAGE_TEX, "Jane_Resume.pdf",
        read_command=ScriptedInput(["d"]), open_editor=lambda p: None,
        open_preview=lambda p: None,
    )
    assert result is not None
    assert result.path == workdir / "Jane_Resume.pdf"
    assert page_count(result.path) == 1


def test_compile_tex_missing_xelatex_raises_latex_error(tmp_path, monkeypatch):
    workdir = recipient_workdir("c", "jane@x.com", root=tmp_path)
    tex_path = write_tex(workdir, ONE_PAGE_TEX)

    def fake_run(*a, **k):
        raise FileNotFoundError("no such file: xelatex")

    monkeypatch.setattr("slap.latex.subprocess.run", fake_run)
    with pytest.raises(LatexError, match="xelatex not found"):
        compile_tex(tex_path)
