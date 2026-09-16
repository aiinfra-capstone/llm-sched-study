"""tools/engine_bench.sh: the refusals, and the record it writes.

A shell script, so coverage cannot see it; these run it for real against stand-ins on PATH.
The benchmark is only meaningful with the GPU to itself, so a running llama-server has to
stop it, and a missing binary has to say where it looked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from support import REPO_ROOT

SCRIPT = REPO_ROOT / "tools" / "engine_bench.sh"

BENCH_JSON = [
    {"n_prompt": 512, "n_gen": 0, "avg_ts": 7000.5, "stddev_ts": 40.1},
    {"n_prompt": 0, "n_gen": 128, "avg_ts": 180.2, "stddev_ts": 1.3},
]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _env(tmp_path: Path, *, llama_server_running: bool) -> dict[str, str]:
    stubs = tmp_path / "stubs"
    _exe(stubs / "pgrep", "exit 0\n" if llama_server_running else "exit 1\n")
    return {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}


def _build(tmp_path: Path, bench: bool = True) -> Path:
    build = tmp_path / "build"
    (build / "bin").mkdir(parents=True)
    if bench:
        _exe(build / "bin" / "llama-bench", f"cat <<'EOF'\n{json.dumps(BENCH_JSON)}\nEOF\n")
    _exe(build / "bin" / "llama-server", "echo 'version: 10569 (1a2b3c4)'\n")
    (build / "bin" / "libllama.so").write_bytes(b"lib")
    (build / "CMakeCache.txt").write_text(
        "GGML_CUDA:BOOL=ON\nCMAKE_BUILD_TYPE:STRING=Release\nOTHER=1\n"
    )
    return build


def _run(tmp_path, build, *, running=False):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"weights")
    out = tmp_path / "bench" / "out.json"
    result = subprocess.run(
        ["bash", str(SCRIPT), str(build), str(model), str(out)],
        env=_env(tmp_path, llama_server_running=running),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, out


def test_it_refuses_while_llama_server_is_running(tmp_path) -> None:
    result, out = _run(tmp_path, _build(tmp_path), running=True)
    assert result.returncode == 1
    assert "llama-server is running" in result.stderr
    assert not out.exists()


def test_it_refuses_without_llama_bench(tmp_path) -> None:
    build = _build(tmp_path, bench=False)
    result, _ = _run(tmp_path, build)
    assert result.returncode == 1
    assert f"no llama-bench at {build}/bin/llama-bench" in result.stderr


def test_it_writes_a_json_record_of_the_build_and_the_results(tmp_path) -> None:
    build = _build(tmp_path)
    result, out = _run(tmp_path, build)
    assert result.returncode == 0, result.stderr
    record = json.loads(out.read_text())
    assert record["results"] == BENCH_JSON
    assert record["llama_server_version"] == "version: 10569 (1a2b3c4)"
    assert record["cmake_cache"] == "GGML_CUDA:BOOL=ON\nCMAKE_BUILD_TYPE:STRING=Release"
    assert set(record["library_sha256"]) == {"libllama.so"}
    assert len(record["model_sha256"]) == 64
    assert "pp512" in result.stdout and "tg128" in result.stdout


def test_it_needs_all_three_arguments(tmp_path) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "build"], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "model path" in result.stderr
