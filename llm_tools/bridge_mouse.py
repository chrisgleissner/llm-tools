"""Mouse-wheel decoding shared by the Bridge curses screens."""
from __future__ import annotations

import curses

# BUTTON4/BUTTON5 are the wheel up/down presses. Builds linked against an
# ncurses without the 5-button mouse ABI lack BUTTON5_PRESSED, so fall back to
# the ncurses mouse-version-2 bit values.
WHEEL_UP = getattr(curses, "BUTTON4_PRESSED", 0x10000)
WHEEL_DOWN = getattr(curses, "BUTTON5_PRESSED", 0x200000)
SHIFT = getattr(curses, "BUTTON_SHIFT", 0x4000000)
# Terminals that report horizontal wheel notches as buttons 6 and 7 (when the
# curses build exposes them at all).
WHEEL_LEFT = getattr(curses, "BUTTON6_PRESSED", 0)
WHEEL_RIGHT = getattr(curses, "BUTTON7_PRESSED", 0)

# Button and wheel events only. REPORT_MOUSE_POSITION would switch the terminal
# to any-motion tracking and send an event for every pointer move, which is
# wasted traffic over SSH and wakes the input loop for nothing.
MOUSE_MASK = getattr(curses, "ALL_MOUSE_EVENTS", 0)


def enable_mouse() -> bool:
    """Ask curses for button and wheel events; ``False`` when the terminal cannot."""
    try:
        curses.mousemask(MOUSE_MASK)
    except (curses.error, AttributeError):
        return False
    return True


def wheel_delta(buttons: int) -> tuple[int, int] | None:
    """Return ``(vertical, horizontal)`` notches for a wheel event, or ``None``.

    Up is -1 and down is +1; left is -1 and right is +1. Shift+wheel is
    reported as horizontal movement, the usual terminal convention. Anything
    that is not a wheel press (clicks, releases, plain motion) gives ``None``.
    """
    if WHEEL_UP and buttons & WHEEL_UP:
        step = -1
    elif WHEEL_DOWN and buttons & WHEEL_DOWN:
        step = 1
    elif WHEEL_LEFT and buttons & WHEEL_LEFT:
        return 0, -1
    elif WHEEL_RIGHT and buttons & WHEEL_RIGHT:
        return 0, 1
    else:
        return None
    if SHIFT and buttons & SHIFT:
        return 0, step
    return step, 0


def read_mouse() -> tuple[int, int, int] | None:
    """``(x, y, button_state)`` of the queued mouse event, or ``None`` if it vanished."""
    try:
        _, x, y, _, buttons = curses.getmouse()
    except curses.error:
        return None
    return x, y, buttons
