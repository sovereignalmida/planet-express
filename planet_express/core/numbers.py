"""Arithmetic on numbers an app sent us, which are not necessarily numbers.

Every widget summarises JSON it did not write. Python makes three things easy to get wrong
with such a value, and each one has cost a widget its whole panel rather than one tile:

  * `True` is an `int`, so a bool sails through an isinstance check and formats as 1;
  * `1e999` decodes to `inf`, and `round(inf)` raises OverflowError;
  * `10**309` decodes to an arbitrary-precision `int`, and `math.isfinite` raises
    OverflowError on it while converting to float -- so the guard itself is the thing that
    breaks.

One place, so a new widget inherits it instead of rediscovering it.
"""

import math

# Comfortably beyond any real count, and inside float range, so arithmetic on two of these
# cannot reach inf.
MAX = 1e12
# Counts and bytes are not the same size of number: 1e12 bytes is 931 GiB, which a photo
# library passes without being remarkable. An exabyte is still ~290 orders of magnitude
# short of float overflow, so the arithmetic guarantee above holds just as well.
MAX_BYTES = 1e18


def finite(value, limit: float = MAX) -> bool:
    """True when `value` is a real number small enough to compute and format with.

    `limit` is the caller's sense of scale: MAX for a count, MAX_BYTES for a size.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and abs(value) <= limit
    except OverflowError:          # an int too large to become a float
        return False


def rounded(value, digits: int = 0):
    """round(), or None when the value is not something to round.

    Callers compute first and round second -- `rounded(average * 1000)` -- so a product that
    overflowed to inf arrives here as the argument and the guard above catches it. round()
    itself cannot overflow anything finite and within MAX, so there is nothing to re-check.
    """
    if not finite(value):
        return None
    return round(value, digits) if digits else round(value)


def ratio(part, whole) -> float | None:
    """part/whole as a percentage, or None when that is not a question with an answer."""
    if not finite(part) or not finite(whole) or not whole:
        return None
    # Both operands finite is not enough: 1e308 over 1e-320 is inf, and a denominator that
    # small is a real thing for a float the app computed rather than counted.
    result = part / whole * 100
    return result if finite(result) else None
