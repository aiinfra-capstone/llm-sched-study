"""Render the .docx requirements spec to Markdown.

The spec is authored in Word because that is what the report needs. Word is also
opaque to git: a one-word change to F-13 shows up in a diff as a rewritten binary
blob, which means a spec edit can land in a PR without either of us being able to
see what it was. This script mirrors the .docx into `requirements-spec.md`, which
IS diffable, and which is also what an outsider reads on GitHub.

The .docx remains the source of truth. The .md is generated — never hand-edit it.

Usage:
    uv run --directory dataplane python ../docs/spec_to_md.py     # regenerate
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
SRC = HERE / "scheduling-requirements-spec.docx"
OUT = HERE / "requirements-spec.md"

BANNER = """<!-- GENERATED FILE — DO NOT EDIT.

Rendered from scheduling-requirements-spec.docx by docs/spec_to_md.py.
The .docx is the source of truth; this mirror exists so that spec changes are
visible in a pull-request diff and readable on GitHub. Regenerate with:

    uv run --directory dataplane python ../docs/spec_to_md.py
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
    args = ap.parse_args()

    if not SRC.exists():
        print(f"error: {SRC} not found", file=sys.stderr)
        return 1

    rendered = render(Document(str(SRC)))

    if args.check:
        current = OUT.read_text() if OUT.exists() else ""
        if current != rendered:
            print(
                f"error: {OUT.name} is stale.\n"
                f"The .docx changed without the Markdown mirror being regenerated, so this\n"
                f"spec edit would not be visible in the PR diff. Run:\n"
                f"    uv run --directory dataplane python ../docs/spec_to_md.py",
                file=sys.stderr,
            )
            return 1
        print(f"{OUT.name} is up to date with {SRC.name}")
        return 0

    OUT.write_text(rendered)
    print(f"wrote {OUT.relative_to(HERE.parent)} ({len(rendered.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
