"""Display-only terminal colorization. Presentation concern ONLY.

HARD REQUIREMENT: nothing here ever touches, wraps, or returns a styled
version of a `subject`/`body`/`stage_bodies` string used for the actual
email. Every function below either PRINTS a styled representation (built
from a *copy* of the caller's data, never mutating it) or returns a plain
`str` for feeding into `input()`'s prompt — never something that gets
staged, templated, or sent to GMass. `_prep_one_recipient` (slap.py) passes
the exact same `subject`/`body` local variables both to `preview_panel()`
here (read-only, display) and to `queue.stage_recipient()` (the real send
path) — this module never sees the send path's variables again after
printing, and never reassigns them.

Uses a single shared `rich.Console` per stream (stdout/stderr). Console
auto-detects a non-TTY (piped/redirected output — e.g. subprocess-captured
test output, or `slap.py list > out.txt`) and disables ANSI color
automatically; nothing here needs to special-case that.
"""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

GREEN = "green"
RED = "bold red"
YELLOW = "yellow"
ACCENT = "bold cyan"

# highlight=False: rich's default ReprHighlighter auto-colors things like
# key=value pairs, quoted strings, and numbers WITHIN a printed line,
# overriding parts of whatever single style was requested (e.g. a red FAIL
# line would show a quoted campaign name in green) — exactly the opposite of
# the intended "one uniform color per message" scheme.
console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)


def success(message: str) -> None:
    console.print(message, style=GREEN)


def error(message: str) -> None:
    """Stdout, red — for inline error reporting that CONTINUES execution
    (e.g. `list`'s per-campaign error, `doctor`'s FAIL lines). Does not
    exit; callers control that."""
    console.print(message, style=RED)


def fail(message: str) -> None:
    """Stderr, red — for the fail-loud-and-exit paths that used to be a
    plain `sys.exit(f"slap: {e}")` (which also writes to stderr). Callers
    still call `sys.exit(1)` themselves afterward — this only prints."""
    err_console.print(message, style=RED)


def warn(message: str) -> None:
    console.print(message, style=YELLOW)


def plain(message: str) -> None:
    console.print(message)


def _rendered(renderable) -> str:
    """Render a rich object to a plain str exactly as the shared console
    would print it (ANSI codes if it's a real TTY, clean plain text
    otherwise) — for composing with input(), which doesn't understand rich
    objects itself."""
    with console.capture() as capture:
        console.print(renderable, end="")
    return capture.get()


def styled_prompt(message: str, *, style: str) -> str:
    """A whole-line styled prompt string, safe to pass straight into
    input()/read_command(...)."""
    return _rendered(Text(message, style=style))


def styled_menu_prompt(items: list) -> str:
    """items: [(letter, rest_of_word), ...], e.g.
    [("r", "ecompile"), ("o", "pen editor"), ("d", "one"), ("a", "bort")]
    -> "[r]ecompile · [o]pen editor · [d]one · [a]bort: " with each
    bracketed letter in the accent color, everything else plain."""
    text = Text()
    for i, (letter, rest) in enumerate(items):
        if i > 0:
            text.append(" · ")
        text.append("[")
        text.append(letter, style=ACCENT)
        text.append(f"]{rest}")
    text.append(": ")
    return _rendered(text)


def preview_panel(recipient: str, subject: str, body: str) -> None:
    """Dim, bordered panel for the email preview — visually separated from
    the surrounding command prompts. Reads subject/body only; never
    modifies or returns them."""
    console.print(
        Panel(
            f"Subject: {subject}\n\n{body}",
            title=f"Preview for {recipient}",
            border_style="dim",
            style="dim",
            padding=(1, 2),
        )
    )
