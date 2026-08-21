"""Render the .docx requirements spec to Markdown.

The spec is authored in Word because that is what the report needs, and shared as
PDF. Neither is diffable: a one-word change to F-13 shows up in git as a rewritten
binary blob, which means a spec edit can land in a PR without either of us being
able to see what it was. This script mirrors the .docx into `requirements-spec.md`,
which IS diffable, and which is also what an outsider reads on GitHub.

The .docx is the source of truth and is kept OUTSIDE this repository. Point this
script at it with --docx, or drop it in docs/ or ~/Downloads/Capstone/ and it will
be found. The .md is generated — never hand-edit it.

Because the source lives outside the repo, `--check` cannot always run: when the
.docx is not reachable it SKIPS rather than fails, so CI stays green. That means the
mirror can drift silently. Regenerate it in the same commit as any spec change.

Usage:
    uv run --directory dataplane python ../docs/spec_to_md.py
    uv run --directory dataplane python ../docs/spec_to_md.py --docx ~/path/spec.docx
    uv run --directory dataplane python ../docs/spec_to_md.py --check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

HERE = Path(__file__).parent
OUT = HERE / "requirements-spec.md"

# The .docx is not committed — it is edited elsewhere and only visits this machine.
SEARCH = [
    HERE / "scheduling-requirements-spec.docx",
    Path.home() / "Downloads" / "Capstone" / "scheduling-requirements-spec.docx",
]


def find_docx() -> Path | None:
    return next((p for p in SEARCH if p.exists()), None)


BANNER = """<!-- GENERATED FILE — DO NOT EDIT.

Rendered from scheduling-requirements-spec.docx by docs/spec_to_md.py.

The .docx is the source of truth and is kept outside this repository; the PDF beside
this file is the shareable copy. This mirror exists so that spec changes are visible
in a pull-request diff and readable inline on GitHub.

Regenerate whenever the spec changes -- in the SAME commit, because CI cannot verify
this file when the .docx is not reachable:

    uv run --directory dataplane python ../docs/spec_to_md.py --docx <path-to.docx>
-->

"""


def iter_blocks(doc: DocxDocument):
    """Yield paragraphs and tables in document order (python-docx won't)."""
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def render_runs(par: Paragraph) -> str:
    """Preserve bold and italic, which the spec uses to mark load-bearing terms."""
    out = []
    for run in par.runs:
        text = run.text
        if not text:
            continue
        lead = len(text) - len(text.lstrip())
        trail = len(text) - len(text.rstrip())
        core = text.strip()
        if core:
            if run.bold:
                core = f"**{core}**"
            if run.italic:
                core = f"*{core}*"
        out.append(text[:lead] + core + text[len(text) - trail :] if trail else text[:lead] + core)
    return "".join(out).strip()


def render_table(table: Table) -> list[str]:
    rows = [
        [c.text.strip().replace("\n", " ").replace("|", "\\|") for c in r.cells] for r in table.rows
    ]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head, *body = rows
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return [*lines, ""]


def render(doc: DocxDocument) -> str:
    lines: list[str] = []
    for block in iter_blocks(doc):
        if isinstance(block, Table):
            lines += render_table(block)
            continue

        text = render_runs(block)
        if not text:
            continue
        style = block.style.name

        if style.startswith("Heading"):
            level = int(style.split()[-1]) if style.split()[-1].isdigit() else 1
            lines += ["", "#" * level + " " + text, ""]
        elif style in {"List Paragraph", "Compact", "List Bullet", "List Number"}:
            lines.append(f"- {text}")
        else:
            lines += [text, ""]

    # collapse runs of blank lines
    cleaned: list[str] = []
    for line in lines:
        if line == "" and cleaned and cleaned[-1] == "":
            continue
        cleaned.append(line)
    return BANNER + "\n".join(cleaned).strip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check", action="store_true", help="exit non-zero if the mirror is stale (for CI)"
    )
    ap.add_argument("--docx", type=Path, help="path to the source .docx")
    args = ap.parse_args()

    src = args.docx or find_docx()

    if src is None or not src.exists():
        searched = "\n  ".join(str(p) for p in ([args.docx] if args.docx else SEARCH))
        if args.check:
            # The source lives outside the repo, so its absence is the normal case in
            # CI. Skipping keeps the build green; the cost is that drift in this file
            # goes undetected, which is why the banner says to regenerate in the same
            # commit as the spec change.
            print(f"skipped: source .docx not reachable. Searched:\n  {searched}")
            return 0
        print(f"error: source .docx not found. Searched:\n  {searched}", file=sys.stderr)
        return 1

    rendered = render(Document(str(src)))

    if args.check:
        current = OUT.read_text() if OUT.exists() else ""
        if current != rendered:
            print(
                f"error: {OUT.name} is stale.\n"
                f"The .docx changed without the Markdown mirror being regenerated, so this\n"
                f"spec edit would not be visible in the PR diff. Run:\n"
                f"    uv run --directory dataplane python ../docs/spec_to_md.py --docx {src}",
                file=sys.stderr,
            )
            return 1
        print(f"{OUT.name} is up to date with {src.name}")
        return 0

    OUT.write_text(rendered)
    print(f"wrote {OUT.relative_to(HERE.parent)} from {src} ({len(rendered.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
