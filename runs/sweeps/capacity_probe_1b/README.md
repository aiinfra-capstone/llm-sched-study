# Capacity probe — the sweep that told me where the knee is

This is the first load sweep I ran on the one-node 1B pool, and it is kept because it is
what the anchor rates were chosen *from* — not because it is an anchor set. Its rates were
picked from a capacity estimate that turned out to be nearly a factor of two high, so two of
its three points sit past the knee and the sweep never brackets the band:

| point | offered | achieved | outcome |
|---|---|---|---|
| light | 0.90 req/s | 0.91 req/s | stable; 2 engine 500s |
| mid | 1.80 req/s | 1.63 req/s | already past the knee; 5.8 s median queue wait |
| heavy | 3.60 req/s | 1.32 req/s | 28 of 200 requests hit the 60 s client ceiling |

Its manifests also predate the change to what `validity.dropped_requests` counts, so their
`valid` flags were computed under the older rule and should not be compared with the anchor
set's. That is another reason these are kept apart from `runs/anchors` rather than merged
into it.

Two things came out of it.

**The pool retires about 1.65 req/s** under this trace mix, measured as 200 requests
retired in 122.6 s while 1.80 req/s was being offered. My estimate from the C-3 table's
concurrency-4 cells had been 2.9 req/s — nearly a factor of two high, because the table
prices a cell at a controlled concurrency and a real mix does not hold concurrency still.
That is the reason the anchor rates are chosen from a measured knee rather than from a
prediction.

**The `engine_error`s have a cause.** `llama-server` returned HTTP 500 with
`"The model produced output that does not match the expected Content-only format"`,
preceded by `common_chat_peg_parse: unparsed Content-only output: <0xB2>`. A lone
continuation byte — the forced-length generation ended mid-character, and this build runs
`/completion` output through the chat content parser because `--jinja` is on by default.
It is a response-formatting artifact, not a capacity limit, and it is the same signature as
the periodic `engine_error`s the Week-2 CPU node produced.

The runs are kept whole (manifest + client log) so both claims can be checked.
