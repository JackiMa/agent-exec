"""Linux execution shim: parent death terminates our dedicated process group.

This improves lifecycle cleanup, not security isolation. A hostile process can
escape a process group; the installed systemd service also owns its cgroup.
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys


def main() -> int:
    parent = int(sys.argv[1])
    argv = sys.argv[2:]
    if not argv:
        return 64

    def stop(_sig: int, _frame: object) -> None:
        os.killpg(os.getpgrp(), signal.SIGKILL)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG failed")
    if os.getppid() != parent:
        return 125
    proc = subprocess.Popen(argv, stdin=sys.stdin.buffer)
    code = proc.wait()
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
