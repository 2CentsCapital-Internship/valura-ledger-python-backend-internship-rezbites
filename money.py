"""Decimal arithmetic, in one place.

Every amount in this book is a `Decimal`. Binary floating point is not merely
discouraged here, it is wrong: the spec rounds each derived amount to the cent
independently and half away from zero, and it deliberately plants cases that
land exactly on a half cent (`partner_rate` may be 0.50, so an odd number of
cents halves onto one). Python's own `round()` is half-to-even and would send
those the other way, as would a float.

So: one `money()`, used by everything, and a hard guard against a float ever
reaching it.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

D = Decimal

ZERO = D("0.00")
CENT = D("0.01")
QTY_STEP = D("0.000001")      # share quantities carry up to 6 decimal places


# Turn text like "10.50" into an exact number. Crashes on purpose if handed a float, because floats lose pennies.
def dec(x) -> Decimal:
    """Decimal from a payload value, refusing floats.

    Payloads deliver numbers as strings. If a float ever appears here it means
    something upstream parsed JSON numbers loosely, and every amount derived
    from it is suspect, so this fails loudly rather than quietly rounding.
    """
    if isinstance(x, float):
        raise TypeError(f"float reached the ledger: {x!r} - use strings")
    if isinstance(x, Decimal):
        return x
    return D(str(x))


# Round to 2 decimal places (pennies), always rounding half a penny UP.
def money(x) -> Decimal:
    """To the cent, half away from zero. The only rounding in this codebase."""
    return dec(x).quantize(CENT, rounding=ROUND_HALF_UP)


# Round a share count to 6 decimal places - you can own part of a share.
def qty(x) -> Decimal:
    """Share quantity, to six places."""
    return dec(x).quantize(QTY_STEP, rounding=ROUND_HALF_UP)


# Turn a number into text like "10.50" so we can send it.
def money_str(x) -> str:
    return format(money(x), "f")


# Turn a share count into plain text, never something like "8E+1".
def qty_str(x) -> str:
    """Plain decimal string, never scientific notation.

    `Decimal.normalize()` trims trailing zeros but turns 80 into 8E+1, which is
    not a plain decimal string; formatting with 'f' puts it back.
    """
    return format(qty(x).normalize(), "f")


# Work out a tiny percentage: "20 bps of 1000" is 2.00.
def bps(principal: Decimal, points) -> Decimal:
    """`points` basis points of `principal`, rounded to the cent.

    1 bps = 0.01%, so 20 bps of 1000.00 is 2.00.
    """
    return money(dec(principal) * dec(points) / D(10000))
