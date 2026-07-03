"""LaTeX compile loop (Build Order step 8).

App-owned compilation (deterministic, gives page count + a guaranteed-correct
attachment) — `code`/Preview are human surfaces only. See SLAP_BUILD_PROMPT.md
§9. This is the one place in the app with a non-overridable hard gate: a
résumé compiling to more than 1 page forces an explicit decision, never a
silent pass-through.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

WORKDIR_ROOT = Path("workdir")


class LatexError(Exception):
    """Raised on fail-loud LaTeX-loop misuse (e.g. xelatex not on PATH)."""


@dataclass
class CompileResult:
    success: bool
    pdf_path: Path = None
    pages: int = None
    log: str = ""


@dataclass
class StagedAttachment:
    path: Path
    attachment_name: str
    tex_hash: str


def recipient_workdir(campaign: str, recipient: str, *, root: Path = WORKDIR_ROOT) -> Path:
    """workdir/<campaign>/<recipient>/ — namespaced per recipient so the
    shared attachment_name can never overwrite across recipients."""
    workdir = root / campaign / recipient
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def write_tex(workdir: Path, tex_source: str) -> Path:
    tex_path = workdir / "resume.tex"
    tex_path.write_text(tex_source, encoding="utf-8")
    return tex_path


def compile_tex(tex_path: Path) -> CompileResult:
    """Runs xelatex twice (cross-references need a second pass). Deletes any
    existing PDF first — otherwise a failed recompile could leave a stale PDF
    from a PRIOR successful compile sitting there looking like success."""
    pdf_path = tex_path.with_suffix(".pdf")
    pdf_path.unlink(missing_ok=True)

    log_parts = []
    returncode = 0
    for _ in range(2):
        try:
            proc = subprocess.run(
                ["xelatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
                cwd=tex_path.parent, capture_output=True, text=True,
            )
        except FileNotFoundError as e:
            raise LatexError(
                "xelatex not found on PATH — run `slap.py doctor` to check system deps"
            ) from e
        log_parts.append(proc.stdout + proc.stderr)
        returncode = proc.returncode
        if returncode != 0:
            break  # no point running pass 2 if pass 1 already failed

    success = returncode == 0 and pdf_path.exists()
    pages = page_count(pdf_path) if success else None
    return CompileResult(success=success, pdf_path=pdf_path if success else None,
                          pages=pages, log="\n".join(log_parts))


def page_count(pdf_path: Path) -> int:
    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception as e:
        raise LatexError(f"could not read page count from {pdf_path}: {e}") from e


def tex_hash(tex_path: Path) -> str:
    return hashlib.sha256(tex_path.read_bytes()).hexdigest()


def stage_final_pdf(pdf_path: Path, tex_path: Path, attachment_name: str) -> StagedAttachment:
    """Rename the freshly-compiled PDF to attachment_name and record a hash
    of the accepted .tex it came from, so a stale/broken PDF can never be
    mistaken for a currently-valid one by a later consumer (§9)."""
    staged_path = pdf_path.parent / attachment_name
    pdf_path.rename(staged_path)
    digest = tex_hash(tex_path)
    staged_path.with_name(staged_path.name + ".hash").write_text(digest, encoding="utf-8")
    return StagedAttachment(path=staged_path, attachment_name=attachment_name, tex_hash=digest)


def abort_workdir(workdir: Path) -> None:
    """abort cleans the workdir; no half-campaign (§9)."""
    shutil.rmtree(workdir)


def open_in_preview(pdf_path: Path) -> None:
    subprocess.run(["open", "-a", "Preview", str(pdf_path)])


def open_in_editor(tex_path: Path) -> None:
    subprocess.run(["code", str(tex_path)])


def _confirm_page_gate(pages: int, read_command) -> bool:
    """The one hard, non-overridable gate (§9): >1 page forces an explicit
    decision. Returns True if the owner explicitly confirmed sending anyway,
    False if they chose to go back and fix it ('r'). No y/n shortcut — the
    owner must type the exact confirmation phrase, naming the page count, so
    an accidental keystroke can never silently pass the gate."""
    phrase = f"send {pages} pages anyway"
    while True:
        decision = read_command(
            f"Résumé is {pages} pages (limit is 1). Type 'r' to go recompile after "
            f"fixing, or type exactly '{phrase}' to confirm sending as-is: "
        ).strip()
        if decision.lower() == "r":
            return False
        if decision.lower() == phrase:
            return True
        print(f"Not understood — type 'r' or exactly: {phrase}")


def run_latex_loop(workdir: Path, tex_source: str, attachment_name: str, *,
                    read_command=input, compile_fn=compile_tex,
                    open_editor=open_in_editor, open_preview=open_in_preview):
    """The interactive [r]ecompile/[o]pen editor/[d]one/[a]bort loop (§9).
    Returns a StagedAttachment on done (after the hard gate resolves), or
    None on abort (workdir cleaned). compile_fn/read_command/open_editor/
    open_preview are injectable for testing without real xelatex/GUI apps."""
    tex_path = write_tex(workdir, tex_source)
    result = compile_fn(tex_path)
    if result.success:
        open_preview(result.pdf_path)
    else:
        print(f"Compile failed:\n{result.log}")
    open_editor(tex_path)

    try:
        while True:
            cmd = read_command("[r]ecompile · [o]pen editor · [d]one · [a]bort: ").strip().lower()
            if cmd == "r":
                result = compile_fn(tex_path)
                if result.success:
                    open_preview(result.pdf_path)
                else:
                    print(f"Compile failed:\n{result.log}")
            elif cmd == "o":
                open_editor(tex_path)
            elif cmd == "d":
                result = compile_fn(tex_path)  # authoritative — never trust a stale prior result
                if not result.success:
                    print(f"Compile failed on final check:\n{result.log}")
                    continue
                if result.pages > 1 and not _confirm_page_gate(result.pages, read_command):
                    continue
                return stage_final_pdf(result.pdf_path, tex_path, attachment_name)
            elif cmd == "a":
                abort_workdir(workdir)
                return None
            else:
                print(f"Unknown command: {cmd!r}")
    except (EOFError, KeyboardInterrupt):
        # Closed/interrupted input (including mid-gate) must never be treated
        # as confirmation — fail closed, loudly, and clean up like an abort.
        print("\nInput closed or interrupted — aborting and cleaning up the workdir.")
        abort_workdir(workdir)
        return None
