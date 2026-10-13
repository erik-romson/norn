"""Keep the TUI's terminal in the raw mode Textual put it in.

A process that shares the terminal — a test script running ``stty``, a
password prompt, a tool restoring terminal settings it saved — can switch it
back to line mode.  The kernel then holds every keypress until Enter, so the
TUI stops seeing keys and a decision modal can no longer be answered.  With
mouse reporting on, the held input also fills the line buffer, and the
terminal rings its bell on every further byte.

It is ``textual``-only and imported lazily by the run screen, never at core
import time.
"""

from __future__ import annotations

import os
import termios

from textual.drivers.linux_driver import LinuxDriver


def restore_raw_mode(fd: int) -> bool:
    """Put the terminal on *fd* back in Textual's raw mode if it has left it.

    Uses Textual's own flag patches so the result matches what its driver set
    at startup.  Input the kernel held in line mode is discarded: it is mouse
    reports and stray keys, and delivering it could answer a modal (an Esc
    aborts the run).

    Returns ``True`` when the mode had to be restored.
    """
    if not os.isatty(fd):
        return False
    attrs = termios.tcgetattr(fd)
    iflag = LinuxDriver._patch_iflag(attrs[0])
    lflag = LinuxDriver._patch_lflag(attrs[3])
    if iflag == attrs[0] and lflag == attrs[3]:
        return False
    attrs[0] = iflag
    attrs[3] = lflag
    attrs[6][termios.VMIN] = 1
    termios.tcflush(fd, termios.TCIFLUSH)
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return True
