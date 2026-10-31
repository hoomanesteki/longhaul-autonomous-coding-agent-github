"""Runs one command in the sandbox, then kills whatever it left running.

The Docker sandbox passes this file's text to `python3 -I -S -c`, so it only
uses the standard library and never runs on the worker. Usage:

    python3 -I -S -c "<this file>" PROGRAM [ARG...]

The program's exit status becomes ours (128 plus the signal number when it died
from a signal). Before we exit, every process that descends from the program is
killed, including daemons that called setsid or forked twice. Linux hands
orphans to the nearest ancestor that is a "child subreaper", so marking
ourselves as one keeps them in our reach. The container's idle process is not
our descendant and is never touched, and neither is another exec's.

Startup time counts, because this runs before every command. Importing signal or
contextlib alone costs about 100 ms in the sandbox (they pull in enum and functools),
so only ctypes is imported beyond the basics, and SIGKILL is written as its number.
"""

import ctypes
import os
import sys
import time

PR_SET_CHILD_SUBREAPER = 36
SIGKILL = 9
# A fork bomb keeps making children, so the sweep gives up after this long.
SWEEP_SECONDS = 5.0


def become_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    # prctl takes unsigned longs. Saying so keeps the call right on every platform.
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        # Carrying on would leave daemons running with no sign that the cleanup failed.
        code = ctypes.get_errno()
        raise OSError(code, f"cannot become a child subreaper: {os.strerror(code)}")


def children() -> list[int]:
    """Processes whose parent is this one, read from /proc."""
    me = os.getpid()
    found: list[int] = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                stat = f.read()
        except OSError:
            continue  # it exited while we looked
        # The command name may hold spaces and parentheses, so cut after the last ")".
        fields = stat[stat.rindex(b")") + 2 :].split()
        if int(fields[1]) == me:
            found.append(int(name))
    return found


def wait_for(child: int) -> int:
    """Wait for `child` and return its status.

    Orphans that exit in the meantime are reaped as they go. Left alone they would
    stay zombies, each holding a slot against the container's pid limit, until the
    command ended.
    """
    while True:
        pid, status = os.waitpid(-1, 0)
        if pid == child:
            return status


def reap() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def sweep() -> None:
    """Kill and reap every descendant. A killed process hands its own children to us."""
    deadline = time.monotonic() + SWEEP_SECONDS
    while time.monotonic() < deadline:
        reap()
        pids = children()
        if not pids:
            return
        for pid in pids:
            # Not contextlib.suppress: importing contextlib is slow here, see the top.
            try:  # noqa: SIM105
                os.kill(pid, SIGKILL)
            except ProcessLookupError:
                pass  # it exited on its own since we listed it
        time.sleep(0.005)


def main(argv: list[str]) -> int:
    become_subreaper()
    child = os.fork()
    if child == 0:
        try:
            os.execv(argv[0], argv)
        except Exception as exc:
            sys.stderr.write(f"cannot run {argv[0]}: {exc}\n")
        os._exit(127)  # the child must never fall through into the code below
    status = wait_for(child)
    sweep()
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
