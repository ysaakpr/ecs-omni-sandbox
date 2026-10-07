"""PID-1 supervisor for the sandbox's host container, with idle stop.

Runs INSIDE the official host image, which doesn't have this package installed:
the task definition passes this file's source to ``python3 -c``. So it must
stay a single, standard-library-only module.

It does three things:

1. Starts its argv (``omnigent host --server …``) as a child, forwards
   SIGTERM/SIGINT to it, and reaps every child, including runner processes
   the host re-parents to PID 1. (What Omnigent's Kubernetes reaper does.)
2. Watches for idleness. The sandbox is idle while there is no runner process
   (no active session here) AND the container's other processes use almost no
   CPU (no background job the agent started is still working).
3. After ``OMNI_ECS_IDLE_STOP_AFTER_S`` of idleness, stops the host cleanly
   and exits 0. The host container is essential, so ECS stops the task and
   billing stops. The home directory stays on EFS, and the next message to the
   session wakes it with a new task.

Settings (environment):
    OMNI_ECS_IDLE_STOP_AFTER_S   idle seconds before stopping; 0 disables
    OMNI_ECS_IDLE_CPU_THRESHOLD  CPU cores below which the container counts as idle
    OMNI_ECS_IDLE_POLL_S         seconds between checks
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time

# How Omnigent names runner processes (checked by tests/test_compat.py).
# A runner is either forked by the zygote (cmdline is the zygote's, and its
# parent is the zygote root) or, without the zygote, started as _entry.
RUNNER_ENTRY = "omnigent.runner._entry"
ZYGOTE = "omnigent.runner._zygote"

STOP_GRACE_S = 60
LOG_PREFIX = "[omni-ecs idle]"


def log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", file=sys.stderr, flush=True)


def read_procs(proc_root: str = "/proc") -> dict[int, tuple[int, str, int]]:
    """``{pid: (ppid, cmdline, cpu_ticks)}`` for every visible process."""
    procs: dict[int, tuple[int, str, int]] = {}
    for name in os.listdir(proc_root):
        if not name.isdigit():
            continue
        try:
            with open(f"{proc_root}/{name}/stat") as f:
                stat = f.read()
            with open(f"{proc_root}/{name}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue  # exited while we were reading
        # The command name in field 2 may contain spaces; parse after the last ")".
        fields = stat[stat.rfind(")") + 2 :].split()
        ppid, utime, stime = int(fields[1]), int(fields[11]), int(fields[12])
        procs[int(name)] = (ppid, cmdline, utime + stime)
    return procs


def descendants(
    procs: dict[int, tuple[int, str, int]], root: int
) -> dict[int, tuple[int, str, int]]:
    """Only *root*'s process tree. In the task's container that's everything
    anyway (the supervisor is PID 1 and adopts orphans); scoping to it keeps
    the check correct wherever else it runs, e.g. on a machine with other
    Omnigent hosts."""
    children: dict[int, list[int]] = {}
    for pid, (ppid, _, _) in procs.items():
        children.setdefault(ppid, []).append(pid)
    tree: dict[int, tuple[int, str, int]] = {}
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in procs and pid not in tree:
            tree[pid] = procs[pid]
            stack.extend(children.get(pid, ()))
    return tree


def classify(
    procs: dict[int, tuple[int, str, int]], *, own_pid: int, host_pid: int
) -> tuple[int, set[int]]:
    """Return (number of runners, pids of Omnigent's always-on processes).

    The always-on processes (this supervisor, the host, the zygote root) are
    left out of the CPU measurement: they keep a little CPU busy forever.
    """
    # The supervisor's own cmdline contains these module names (its source is
    # passed with -c), so it and the host are never counted as runners.
    candidates = {pid: cmd for pid, (_, cmd, _) in procs.items() if pid not in (own_pid, host_pid)}
    zygotes = {pid for pid, cmd in candidates.items() if ZYGOTE in cmd}
    roots = {pid for pid in zygotes if procs[pid][0] not in zygotes}
    runners = sum(
        1
        for pid, cmd in candidates.items()
        if RUNNER_ENTRY in cmd or (pid in zygotes and pid not in roots)
    )
    return runners, {own_pid, host_pid, *roots}


class IdleTracker:
    """Decides when the sandbox has been idle long enough to stop."""

    def __init__(self, *, idle_after_s: float, cpu_threshold: float, now: float) -> None:
        self.idle_after_s = idle_after_s
        self.cpu_threshold = cpu_threshold
        self.clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        # Counting starts at boot, so a host that never gets a runner still stops.
        self.idle_since = now
        self._last: tuple[float, int] | None = None

    def observe(
        self,
        procs: dict[int, tuple[int, str, int]],
        *,
        runners: int,
        infra: set[int],
        now: float,
    ) -> bool:
        """Record one sample; return True when the idle window has passed."""
        ticks = sum(cpu for pid, (_, _, cpu) in procs.items() if pid not in infra)
        busy = runners > 0
        if self._last is not None and now > self._last[0]:
            # Processes that exit take their ticks with them; clamp at zero.
            cores = max(0, ticks - self._last[1]) / self.clk_tck / (now - self._last[0])
            busy = busy or cores >= self.cpu_threshold
        self._last = (now, ticks)
        if busy:
            self.idle_since = now
        return now - self.idle_since >= self.idle_after_s


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        log(f"ignoring invalid {name}; using {default}")
        return default


def main(argv: list[str]) -> int:
    if not argv:
        log("usage: supervisor <command> [args...]")
        return 2
    idle_after = _env_float("OMNI_ECS_IDLE_STOP_AFTER_S", 900)
    threshold = _env_float("OMNI_ECS_IDLE_CPU_THRESHOLD", 0.05)
    poll = max(0.05, _env_float("OMNI_ECS_IDLE_POLL_S", 30))

    child = subprocess.Popen(argv)  # noqa: S603 - argv is the task's own command

    def forward(signum: int, _frame: object) -> None:
        with contextlib.suppress(ProcessLookupError):
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    if idle_after > 0:
        log(f"stopping after {idle_after:.0f}s with no session and CPU < {threshold} cores")
    tracker = IdleTracker(idle_after_s=idle_after, cpu_threshold=threshold, now=time.monotonic())
    next_check = time.monotonic() + poll
    stopping_since: float | None = None

    while True:
        # Reap everything; return when the host itself has exited.
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return child.returncode or 0
            if pid == 0:
                break
            if pid == child.pid:
                code = os.waitstatus_to_exitcode(status)
                if stopping_since is not None:
                    log("host stopped; exiting so ECS stops the task")
                    return 0
                return 128 - code if code < 0 else code

        now = time.monotonic()
        if stopping_since is not None and now - stopping_since > STOP_GRACE_S:
            log(f"host still running {STOP_GRACE_S}s after SIGTERM; killing it")
            child.kill()
        elif stopping_since is None and idle_after > 0 and now >= next_check:
            next_check = now + poll
            procs = descendants(read_procs(), os.getpid())
            runners, infra = classify(procs, own_pid=os.getpid(), host_pid=child.pid)
            if tracker.observe(procs, runners=runners, infra=infra, now=now):
                log(f"idle for {now - tracker.idle_since:.0f}s; stopping the host")
                stopping_since = now
                child.send_signal(signal.SIGTERM)
        time.sleep(min(1.0, poll))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
