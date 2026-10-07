"""The in-task supervisor: process classification, idle decisions, and the real
process lifecycle (run against a fake host with short timings)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from omnigent.community.sandbox.ecs import supervisor
from omnigent.community.sandbox.ecs.config import EcsSandboxConfig
from omnigent.community.sandbox.ecs.taskdef import build_task_definition
from tests.conftest import base_config

SUPERVISOR_SRC = Path(supervisor.__file__).read_text()


# ── classification and idle logic ────────────────────────────────────


def test_classify_counts_forked_and_direct_runners() -> None:
    procs = {
        1: (0, "python3 -c <supervisor> omnigent host", 0),
        10: (1, "/opt/venv/bin/python3 /opt/venv/bin/omnigent host --server https://s", 0),
        11: (10, "python3 -P -m omnigent.runner._zygote", 0),  # zygote root
        12: (11, "python3 -P -m omnigent.runner._zygote", 0),  # forked runner
        13: (10, "python3 -m omnigent.runner._entry", 0),  # runner without zygote
        14: (12, "claude --permission-mode auto", 0),
    }
    runners, infra = supervisor.classify(procs, own_pid=1, host_pid=10)
    assert runners == 2
    assert infra == {1, 10, 11}


def test_descendants_ignores_processes_outside_the_tree() -> None:
    procs = {
        1: (0, "init", 0),
        5: (1, "python3 -m omnigent.runner._entry", 0),  # another host's runner
        20: (1, "supervisor", 0),
        21: (20, "omnigent host", 0),
        22: (21, "worker", 0),
    }
    assert set(supervisor.descendants(procs, 20)) == {20, 21, 22}


def test_supervisor_and_host_never_count_as_runners() -> None:
    # The supervisor's source (passed with -c) contains the runner module names.
    procs = {
        1: (0, f"python3 -c ...{supervisor.RUNNER_ENTRY}...{supervisor.ZYGOTE}...", 0),
        10: (1, f"python3 -c ...{supervisor.RUNNER_ENTRY}...", 0),
    }
    assert supervisor.classify(procs, own_pid=1, host_pid=10) == (0, {1, 10})


def test_idle_host_with_only_the_zygote_has_no_runners() -> None:
    procs = {
        1: (0, "supervisor", 0),
        10: (1, "omnigent host", 0),
        11: (10, "python3 -P -m omnigent.runner._zygote", 0),
    }
    assert supervisor.classify(procs, own_pid=1, host_pid=10) == (0, {1, 10, 11})


def _tracker(idle_after: float = 100, threshold: float = 0.05) -> supervisor.IdleTracker:
    tracker = supervisor.IdleTracker(idle_after_s=idle_after, cpu_threshold=threshold, now=0)
    tracker.clk_tck = 100
    return tracker


def test_runner_presence_keeps_the_sandbox_awake() -> None:
    tracker = _tracker()
    assert not tracker.observe({}, runners=1, infra=set(), now=50)
    assert not tracker.observe({}, runners=1, infra=set(), now=140)
    assert not tracker.observe({}, runners=0, infra=set(), now=200)  # idle since 140
    assert tracker.observe({}, runners=0, infra=set(), now=240)


def test_background_cpu_keeps_the_sandbox_awake_but_infra_cpu_does_not() -> None:
    tracker = _tracker()
    # pid 5 is a background job; pid 10 is the host (infra) and always ticks.
    tracker.observe({5: (1, "job", 0), 10: (1, "host", 0)}, runners=0, infra={10}, now=0)
    # Job used 50 ticks over 10s at 100 ticks/s: 0.05 cores, right at the threshold.
    assert not tracker.observe(
        {5: (1, "job", 50), 10: (1, "host", 9999)}, runners=0, infra={10}, now=10
    )
    assert tracker.idle_since == 10
    # Job stopped working; only the host keeps ticking.
    tracker.observe({5: (1, "job", 50), 10: (1, "host", 20000)}, runners=0, infra={10}, now=60)
    assert tracker.observe(
        {5: (1, "job", 50), 10: (1, "host", 30000)}, runners=0, infra={10}, now=110
    )


def test_exited_process_does_not_count_as_negative_cpu() -> None:
    tracker = _tracker(idle_after=10)
    tracker.observe({5: (1, "job", 1000)}, runners=0, infra=set(), now=0)
    assert tracker.observe({}, runners=0, infra=set(), now=10)


def test_read_procs_parses_stat_with_spaces_in_the_name(tmp_path: Path) -> None:
    proc = tmp_path / "42"
    proc.mkdir()
    # Fields after the name: state ppid pgrp session tty tpgid flags minflt
    # cminflt majflt cmajflt utime stime ...
    (proc / "stat").write_text("42 (my (odd) name) S 7 42 42 0 -1 0 0 0 0 0 30 12 0 0\n")
    (proc / "cmdline").write_bytes(b"python3\0-m\0omnigent.runner._entry\0")
    (tmp_path / "self").mkdir()  # non-numeric entries are skipped
    assert supervisor.read_procs(str(tmp_path)) == {
        42: (7, "python3 -m omnigent.runner._entry", 42)
    }


def test_read_procs_works_on_the_real_proc() -> None:
    procs = supervisor.read_procs()
    assert os.getpid() in procs and "python" in procs[os.getpid()][1]


# ── task definition wiring ───────────────────────────────────────────


def _host_container(**overrides: object) -> dict:
    td = build_task_definition(
        EcsSandboxConfig(**base_config(**overrides)),
        sandbox_id="omni-sb-1234abcd",
        host_id="0123456789abcdef0123456789abcdef",
        host_name="managed-01234567",
        server_url="https://omnigent.example.com",
        token_secret_arn="arn:aws:secretsmanager:ap-south-1:123456789012:secret:omnigent-ecs/sb/x",
    )
    return next(c for c in td["containerDefinitions"] if c["name"] == "host")


def test_host_runs_under_the_supervisor_with_idle_settings() -> None:
    host = _host_container(idle_stop_after_s=600, idle_cpu_threshold=0.1)
    script = host["command"][2]
    assert host["command"][:2] == ["bash", "-lc"]
    assert script.startswith("exec python3 -c ") and "class IdleTracker" in script
    assert script.endswith("omnigent host --server https://omnigent.example.com")
    env = {e["name"]: e["value"] for e in host["environment"]}
    assert env["OMNI_ECS_IDLE_STOP_AFTER_S"] == "600"
    assert env["OMNI_ECS_IDLE_CPU_THRESHOLD"] == "0.1"


def test_idle_stop_defaults_on_and_can_be_disabled() -> None:
    env = {e["name"]: e["value"] for e in _host_container()["environment"]}
    assert env["OMNI_ECS_IDLE_STOP_AFTER_S"] == "900"
    env = {e["name"]: e["value"] for e in _host_container(idle_stop_after_s=0)["environment"]}
    assert env["OMNI_ECS_IDLE_STOP_AFTER_S"] == "0"


def test_supervisor_source_fits_in_a_task_definition() -> None:
    # Task definitions are capped at 64 KiB; keep plenty of room.
    assert len(SUPERVISOR_SRC.encode()) < 16 * 1024


# ── real processes ───────────────────────────────────────────────────

# A stand-in for `omnigent host`: starts the children the test asks for,
# records SIGTERM, then exits like the real host does.
FAKE_HOST = textwrap.dedent(
    """
    import signal, subprocess, sys, time
    marker, mode, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
    def stop(*_):
        open(marker, "w").write("sigterm")
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    if mode == "runner":
        subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})",
                          "omnigent.runner._entry"])
    elif mode == "cpu":
        burn = f"import time\\nend = time.time() + {seconds}\\nwhile time.time() < end: pass"
        subprocess.Popen([sys.executable, "-c", burn])
    elif mode == "exit":
        time.sleep(seconds)
        sys.exit(3)
    while True:
        time.sleep(0.05)
    """
)


def _run(tmp_path: Path, mode: str, seconds: float, idle_after: float) -> tuple[int, float, Path]:
    marker = tmp_path / "terminated"
    env = {
        **os.environ,
        "OMNI_ECS_IDLE_STOP_AFTER_S": str(idle_after),
        "OMNI_ECS_IDLE_POLL_S": "0.1",
    }
    start = time.monotonic()
    proc = subprocess.run(  # noqa: S603 - fixed test command
        [
            sys.executable,
            "-c",
            SUPERVISOR_SRC,
            sys.executable,
            "-c",
            FAKE_HOST,
            str(marker),
            mode,
            str(seconds),
        ],  # fmt: skip
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, time.monotonic() - start, marker


def test_stops_the_host_once_the_runner_is_gone_and_idle(tmp_path: Path) -> None:
    code, elapsed, marker = _run(tmp_path, "runner", seconds=1.5, idle_after=1.0)
    assert code == 0
    assert marker.read_text() == "sigterm"
    assert elapsed >= 1.5 + 1.0 - 0.2  # never while the runner was alive


def test_busy_background_job_delays_the_stop(tmp_path: Path) -> None:
    code, elapsed, marker = _run(tmp_path, "cpu", seconds=2.5, idle_after=0.8)
    assert code == 0 and marker.exists()
    assert elapsed >= 2.5 + 0.8 - 0.3


def test_disabled_idle_stop_only_supervises(tmp_path: Path) -> None:
    code, elapsed, marker = _run(tmp_path, "exit", seconds=1.0, idle_after=0)
    assert code == 3  # the host's own exit code is passed through
    assert not marker.exists()


def test_sigterm_is_forwarded_to_the_host(tmp_path: Path) -> None:
    marker = tmp_path / "terminated"
    proc = subprocess.Popen(  # noqa: S603 - fixed test command
        [
            sys.executable,
            "-c",
            SUPERVISOR_SRC,
            sys.executable,
            "-c",
            FAKE_HOST,
            str(marker),
            "idle",
            "0",
        ],  # fmt: skip
        env={**os.environ, "OMNI_ECS_IDLE_STOP_AFTER_S": "0"},
    )
    time.sleep(1.0)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) == 0
    assert marker.read_text() == "sigterm"


@pytest.mark.parametrize("value", ["abc", ""])
def test_bad_settings_fall_back_to_defaults(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_ECS_IDLE_POLL_S", value)
    assert supervisor._env_float("OMNI_ECS_IDLE_POLL_S", 30) == 30
