from __future__ import annotations

import os
import termios
import time

import pytest
from textual.drivers.linux_driver import LinuxDriver

from norn.tui.terminal import restore_raw_mode

_LINE_MODE = termios.ICANON | termios.ECHO


@pytest.fixture
def pty_pair():
    """A pty whose slave end is in Textual's raw mode: ``(master, slave)``."""
    master, slave = os.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[0] = LinuxDriver._patch_iflag(attrs[0])
    attrs[3] = LinuxDriver._patch_lflag(attrs[3])
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    yield master, slave
    os.close(slave)
    os.close(master)


def _switch_to_line_mode(fd: int) -> None:
    """What ``stty icanon echo`` in a child sharing the terminal does."""
    attrs = termios.tcgetattr(fd)
    attrs[3] |= _LINE_MODE
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def test_raw_terminal_is_left_alone(pty_pair):
    _, slave = pty_pair
    assert restore_raw_mode(slave) is False


def test_line_mode_set_by_a_child_is_switched_back_to_raw(pty_pair):
    _, slave = pty_pair
    _switch_to_line_mode(slave)

    assert restore_raw_mode(slave) is True
    assert termios.tcgetattr(slave)[3] & _LINE_MODE == 0


def test_input_held_in_line_mode_is_discarded(pty_pair):
    master, slave = pty_pair
    _switch_to_line_mode(slave)
    # A mouse report and an Esc typed while line mode held them back.
    os.write(master, b"\x1b[<35;10;20M\x1b")
    time.sleep(0.05)

    restore_raw_mode(slave)

    os.set_blocking(slave, False)
    with pytest.raises(BlockingIOError):
        os.read(slave, 1024)


def test_a_non_terminal_is_ignored(tmp_path):
    with open(tmp_path / "out", "w") as f:
        assert restore_raw_mode(f.fileno()) is False
