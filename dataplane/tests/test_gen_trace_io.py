"""Reading a trace back, and the CLI that writes one.

The loader is a version gate (§12.2) and a hash gate. Both exist because the trace's
SHA-256 is its identity everywhere downstream — the manifest carries it, the replay client
refuses to start without it, and Aditya's DES reads the same file. A loader that guesses
at an unknown version, or accepts a file whose bytes do not match the manifest, turns that
identity into decoration.

The CLI is tested through `main()` rather than as a subprocess: it is the entry point I
actually use to make traces on a node, and its `--model` flag is what stamps a trace with
the tokenizer facts of the run set it belongs to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataplane.harness import gen_trace


def test_load_returns_the_header_and_body_separately(trace_config, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    sha = gen_trace.generate(trace_config(), path)
    header, body = gen_trace.load(path, expect_sha256=sha)
    assert header["record"] == "header"
    assert all(r["record"] == "req" for r in body)
    assert len(body) == header["n_requests"]


def test_hash_mismatch_is_refused(trace_config, tmp_path: Path) -> None:
    """The manifest's `trace_sha256` is a claim about bytes. If it is wrong, every result
    joined against that manifest is attributed to the wrong workload."""
    path = tmp_path / "t.jsonl"
    gen_trace.generate(trace_config(), path)
    with pytest.raises(ValueError, match="sha256 mismatch"):
        gen_trace.load(path, expect_sha256="0" * 64)


def test_returned_hash_is_the_hash_of_the_file(trace_config, tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "t.jsonl"
    sha = gen_trace.generate(trace_config(), path)
    assert sha == hashlib.sha256(path.read_bytes()).hexdigest()


def test_unknown_trace_schema_is_rejected_loudly(trace_config, tmp_path: Path) -> None:
    """§12.2's mitigation for schema drift, on the read side. A loader that shrugged at
    version 2 would produce a figure six weeks later that nobody could explain."""
    path = tmp_path / "t.jsonl"
    gen_trace.generate(trace_config(), path)
    lines = path.read_text().splitlines()
    header = json.loads(lines[0])
    header["trace_schema"] = 99
    path.write_text("\n".join([json.dumps(header, separators=(",", ":")), *lines[1:]]) + "\n")

    with pytest.raises(ValueError, match="refusing to guess"):
        gen_trace.load(path)


def test_a_file_without_a_header_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"record":"req","req_id":"r000001"}\n')
    with pytest.raises(ValueError, match="not a header"):
        gen_trace.load(path)


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("")
    with pytest.raises(ValueError, match="not a header"):
        gen_trace.load(path)


def test_a_truncated_body_is_refused(trace_config, tmp_path: Path) -> None:
    """A trace cut short by a full disk still parses line by line. The header/body count
    cross-check is the only thing that notices."""
    path = tmp_path / "t.jsonl"
    gen_trace.generate(trace_config(n_requests=12, duration_s=10_000), path)
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-3]) + "\n")
    with pytest.raises(ValueError, match="header claims"):
        gen_trace.load(path)


def test_blank_lines_do_not_count_as_records(trace_config, tmp_path: Path) -> None:
    """Trailing whitespace from an editor must not make a valid trace look truncated."""
    path = tmp_path / "t.jsonl"
    gen_trace.generate(trace_config(), path)
    with path.open("a") as fh:
        fh.write("\n\n")
    header, body = gen_trace.load(path)
    assert len(body) == header["n_requests"]


def test_generate_creates_missing_parent_directories(trace_config, tmp_path: Path) -> None:
    """`gen-trace -o runs/2026-08-29/t.jsonl` should not need a mkdir first."""
    path = tmp_path / "deep" / "nested" / "t.jsonl"
    gen_trace.generate(trace_config(), path)
    assert path.exists()


# --------------------------------------------------------------------------------------
# The CLI
# --------------------------------------------------------------------------------------


def _run_cli(monkeypatch, args: list[str]) -> int:
    monkeypatch.setattr("sys.argv", ["gen-trace", *args])
    return gen_trace.main()


def test_cli_writes_a_loadable_trace(trace_config, tmp_path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(trace_config()))
    out = tmp_path / "t.jsonl"

    assert _run_cli(monkeypatch, [str(config_path), "-o", str(out)]) == 0

    printed = capsys.readouterr().out
    sha = printed.split("sha256")[1].split()[0]
    header, body = gen_trace.load(out, expect_sha256=sha)
    assert len(body) == header["n_requests"]


def test_cli_seed_override_changes_the_trace(trace_config, tmp_path, monkeypatch) -> None:
    """`--seed` is how a run set gets its replicates. It has to actually re-roll."""
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(trace_config()))
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"

    _run_cli(monkeypatch, [str(config_path), "-o", str(a), "--seed", "1"])
    _run_cli(monkeypatch, [str(config_path), "-o", str(b), "--seed", "2"])
    assert a.read_bytes() != b.read_bytes()


def test_cli_model_flag_stamps_the_tokenizer_facts(trace_config, tmp_path, monkeypatch) -> None:
    """The model is held constant inside a pool (F-9); `--model` picks which run set the
    trace belongs to. It must rewrite vocab_size, not just record a name."""
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(trace_config()))
    out = tmp_path / "t.jsonl"

    _run_cli(monkeypatch, [str(config_path), "-o", str(out), "--model", "mistral-7b-v03"])

    header, _ = gen_trace.load(out)
    assert header["vocab_size"] == gen_trace.MODELS["mistral-7b-v03"]["vocab_size"]
    assert header["reserved_ids_excluded"] is False


def test_cli_rejects_a_model_it_has_no_tokenizer_facts_for(
    trace_config, tmp_path, monkeypatch
) -> None:
    """argparse `choices` is the gate; a model with no row in the table cannot be stamped."""
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(trace_config()))
    with pytest.raises(SystemExit):
        _run_cli(
            monkeypatch, [str(config_path), "-o", str(tmp_path / "t.jsonl"), "--model", "gpt9"]
        )
