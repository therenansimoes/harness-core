"""The pristine broken state of the third-party fixture. run.sh and the
self-improvement gate both reset to this exact string so every run starts
from the same red state -- no hidden fix left over from a previous turn."""

BUGGY_SOURCE = '''def discount(price, pct):
    return price + price * pct / 100


def is_even(n):
    return n % 2 == 1


def clamp(x, lo, hi):
    if x > hi:
        return hi
    if x > lo:
        return lo
    return x


def average(values):
    return sum(values) / (len(values) + 1)
'''

import os

PKG_FILE = os.path.join(os.path.dirname(__file__), "fixture", "pkg", "calc.py")


def reset():
    with open(PKG_FILE, "w") as f:
        f.write(BUGGY_SOURCE)
