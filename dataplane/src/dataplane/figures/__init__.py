"""Figure scripts. Consume Parquet only, never raw logs.

F-24: read `manifest.vehicle` and stamp simulated plots automatically. Labelling
by hand fails exactly once, in the final report.

Everything is implemented in `plots`; this module is the flat surface the rest of the
repository imports, so a caller never has to know which file a figure lives in.
"""

from dataplane.figures.plots import (
    CELLS,
    FIGURES,
    achieved_rps,
    analysable,
    annotations,
    by_offered_load,
    caption,
    eligible,
    example_frame,
    example_sweep,
    h1_interaction,
    h2_advantage_curve,
    h3_axis,
    main,
    percentile,
    render,
    render_many,
    render_set,
    stamp,
    stamp_text,
    vehicle_of,
)

__all__ = [
    "CELLS",
    "FIGURES",
    "achieved_rps",
    "analysable",
    "annotations",
    "by_offered_load",
    "caption",
    "eligible",
    "example_frame",
    "example_sweep",
    "h1_interaction",
    "h2_advantage_curve",
    "h3_axis",
    "main",
    "percentile",
    "render",
    "render_many",
    "render_set",
    "stamp",
    "stamp_text",
    "vehicle_of",
]
