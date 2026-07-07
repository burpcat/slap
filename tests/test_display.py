"""Display-only colorization tests. HARD REQUIREMENT under test: rich
styling here is a presentation concern only — it must degrade cleanly to
plain text on a non-TTY, and it must never be able to leak an ANSI escape
into anything that isn't a print-time rendering (that end-to-end guarantee,
for the actual staged/sent message body, is tested in test_cli_skeleton.py;
this file covers slap/display.py's own functions in isolation).

Every test here explicitly forces (or forbids) color via a dedicated
`rich.Console(force_terminal=...)` swapped into `slap.display`'s module-level
console — never relying on the ambient environment's own TTY/FORCE_COLOR
state, which is inherently non-deterministic across machines/CI.
"""
from rich.console import Console

from slap import display

ESC = "\x1b"


def make_console(*, force_terminal: bool, stderr: bool = False) -> Console:
    return Console(force_terminal=force_terminal, no_color=not force_terminal, stderr=stderr,
                    highlight=False)


def test_success_writes_green_to_stdout_console_when_colored(monkeypatch, capsys):
    monkeypatch.setattr(display, "console", make_console(force_terminal=True))
    display.success("Staged jane@acme.com.")
    out = capsys.readouterr().out
    assert ESC in out
    assert "Staged jane@acme.com." in out


def test_success_is_plain_text_when_not_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(display, "console", make_console(force_terminal=False))
    display.success("Staged jane@acme.com.")
    out = capsys.readouterr().out
    assert ESC not in out
    assert out.strip() == "Staged jane@acme.com."


def test_the_real_module_consoles_disable_the_default_highlighter():
    # Real bug caught while producing the visual demo: rich's Console has a
    # default ReprHighlighter that auto-colors things LOOKING like
    # key=value pairs, quoted strings, and numbers within a printed line —
    # e.g. a red FAIL line like "campaign 'x': FAIL" would show 'x' in a
    # DIFFERENT color than the rest, defeating "one uniform color per
    # message type". display.py's actual module-level consoles (not a
    # test-swapped-in one — this checks the real singletons) must be
    # constructed with highlight=False. Checks rich's own internal flag
    # directly since that's the one thing print-based assertions can't pin
    # down without also controlling the console itself (which would just
    # test the test, not slap/display.py's real construction).
    assert display.console._highlight is False
    assert display.err_console._highlight is False


def test_highlight_false_produces_one_uniform_style_not_a_multi_colored_line():
    # Demonstrates the actual mechanism the property check above relies on:
    # with highlighting disabled, a styled line is wrapped in exactly one
    # SGR sequence, not split into several by auto-detected key=value/
    # quoted-string/number patterns.
    with_highlight = Console(force_terminal=True, no_color=False)
    without_highlight = Console(force_terminal=True, no_color=False, highlight=False)
    message = "campaign 'coldpost': FAIL"

    with with_highlight.capture() as cap:
        with_highlight.print(message, style=display.RED, end="")
    assert cap.get() != f"\x1b[1;31m{message}\x1b[0m"  # rich's default DOES multi-color it

    with without_highlight.capture() as cap:
        without_highlight.print(message, style=display.RED, end="")
    assert cap.get() == f"\x1b[1;31m{message}\x1b[0m"  # exactly one uniform wrap


def test_error_writes_to_stdout_not_stderr(monkeypatch, capsys):
    # error() is for inline reporting that continues execution (list's
    # per-campaign ERROR, doctor's FAIL lines) — those are stdout today,
    # colorization must not silently move them to stderr.
    monkeypatch.setattr(display, "console", make_console(force_terminal=False))
    display.error("broken-campaign: ERROR — bad yaml")
    captured = capsys.readouterr()
    assert "broken-campaign: ERROR" in captured.out
    assert captured.err == ""


def test_fail_writes_to_stderr_not_stdout(monkeypatch, capsys):
    # fail() replaces the old sys.exit(f"slap: {e}") pattern, which wrote to
    # stderr — must keep doing so, colorized or not.
    monkeypatch.setattr(display, "err_console", make_console(force_terminal=False, stderr=True))
    display.fail("slap: config.yaml not found")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "slap: config.yaml not found" in captured.err


def test_warn_is_plain_text_when_not_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(display, "console", make_console(force_terminal=False))
    display.warn("⚠ SOFT WARN: 2 other contact(s) already on domain acme.com:")
    out = capsys.readouterr().out
    assert ESC not in out
    assert "SOFT WARN" in out


def test_styled_prompt_plain_text_is_byte_identical_to_the_original(monkeypatch):
    monkeypatch.setattr(display, "console", make_console(force_terminal=False))
    original = "Proceed anyway? [y/N]: "
    assert display.styled_prompt(original, style=display.YELLOW) == original


def test_styled_prompt_contains_ansi_when_forced(monkeypatch):
    monkeypatch.setattr(display, "console", make_console(force_terminal=True))
    result = display.styled_prompt("Proceed anyway? [y/N]: ", style=display.YELLOW)
    assert ESC in result
    assert "Proceed anyway? [y/N]: " in result  # text content preserved, just wrapped


def test_styled_menu_prompt_plain_text_matches_original_layout(monkeypatch):
    monkeypatch.setattr(display, "console", make_console(force_terminal=False))
    result = display.styled_menu_prompt(
        [("r", "ecompile"), ("o", "pen editor"), ("d", "one"), ("a", "bort")]
    )
    assert result == "[r]ecompile · [o]pen editor · [d]one · [a]bort: "


def test_styled_menu_prompt_colors_only_the_bracketed_letters(monkeypatch):
    monkeypatch.setattr(display, "console", make_console(force_terminal=True))
    result = display.styled_menu_prompt([("r", "ecompile"), ("d", "one")])
    assert ESC in result
    # the plain text is still fully present and in order once ANSI is stripped
    import re
    stripped = re.sub(r"\x1b\[[0-9;]*m", "", result)
    assert stripped == "[r]ecompile · [d]one: "


def test_preview_panel_does_not_mutate_or_return_subject_or_body(monkeypatch, capsys):
    monkeypatch.setattr(display, "console", make_console(force_terminal=True))
    subject = "Quick note about the Data Scientist role at Acme"
    body = "Hi Acme team,\n\nI came across the role and wanted to reach out.\n"
    subject_copy, body_copy = subject, body

    result = display.preview_panel("jane@acme.com", subject, body)

    assert result is None  # display-only: nothing returned to accidentally reuse
    assert subject == subject_copy  # caller's variables are untouched
    assert body == body_copy
    out = capsys.readouterr().out
    assert "jane@acme.com" in out
    assert "Data Scientist" in out
