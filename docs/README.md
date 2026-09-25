# Documentation

This set was written on **2026-09-16** and supersedes every planning, scope and results
document that came before it. The earlier documents are in the git history and are not
authoritative for anything: the base-scope record, the elevation-1 scope and workplan, the
checkpoint and the writing brief all folded into the six documents below.

Everything we do from here refers to these. They are edited in place rather than replaced by
new documents beside them.

| Document | What it settles | Changes when |
|---|---|---|
| [research-plan.md](research-plan.md) | The question, the claims ladder, the hypotheses that are in and out, the scope, and the deviations from the frozen specification | A measurement contradicts a premise in it, by the procedure in its section 8 |
| [analysis-plan.md](analysis-plan.md) | How every number is computed, and what each campaign outcome licenses us to say. Frozen before the campaigns run | Only through its amendment log, with the measurement that forced the change |
| [experiment-plan.md](experiment-plan.md) | What we run, in what order, what gates each block, and what we refuse to run | After every campaign, in place |
| [system-design.md](system-design.md) | The instrument: the seam, the contracts, one run end to end, the policies, the invariants, and the known limits | When the instrument changes |
| [test-plan.md](test-plan.md) | What the suite has to guarantee, and what passing means | When a new behaviour needs a guarantee |
| [results.md](results.md) | Every measurement that stands, with provenance, and every claim withdrawn | After every campaign |

## The frozen specification

`base_scope/scheduling-requirements-spec.pdf` is the authority for the original scope and is
never edited. Where this set differs from it, the difference is listed in `research-plan.md`
section 7 and goes into the paper's method section. A text extraction of the PDF is not
committed, because a text mirror went stale inside one cycle the last time.

## Figures

`uml/` holds the twelve-figure set: PlantUML sources, `render.sh`, and the captions in
`FIGURES.md`. The paper's measurement figures are drawn by `tools/paper_figures.py` from the
re-derived summaries and are listed in `results.md`.

## Where the numbers live

Each run set's `summary.json` is the source of truth for every number in `results.md`. Those
files and every run manifest are committed, so a number in the write-up can be opened by
someone who does not have our disks.
