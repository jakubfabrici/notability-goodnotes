"""Notability ``.note`` codec: keyed-archive helpers, reader and writer."""

# Notability's paper does not start at document x = 0: its left edge sits at
# ``-W * 20 / 768`` document units (2.6 % of the page width) and the paper spans ``W`` units
# from there.  Notability's own PDF exports draw ink under the matrix
# ``612/W 0 0 612/W 15.9375 ...`` (15.9375 = 612 * 20 / 768) and PDF backgrounds at x = 0,
# so this inset applies to plain and PDF-backed pages alike (docs/notability-format.md 5.1).
X_INSET_FRACTION = 20.0 / 768.0


def x_inset(page_width: float) -> float:
    """Document x of the paper's left edge for a note of width ``page_width`` (negative)."""
    return -float(page_width) * X_INSET_FRACTION
