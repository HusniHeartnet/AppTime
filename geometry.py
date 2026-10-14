"""Rectangle math for working out how much of each window is visible.

Rectangles are (left, top, right, bottom) in screen coordinates.
"""

# A window counts as "on screen" when at least this much of it is uncovered.
MIN_VISIBLE_FRACTION = 0.15
MIN_VISIBLE_PIXELS = 200 * 200


def area(r):
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


def intersect(a, b):
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def subtract(rects, cut):
    """Remove `cut` from a list of rectangles, splitting them as needed."""
    out = []
    for r in rects:
        i = intersect(r, cut)
        if area(i) == 0:
            out.append(r)
            continue
        left, top, right, bottom = r
        if i[1] > top:
            out.append((left, top, right, i[1]))
        if i[3] < bottom:
            out.append((left, i[3], right, bottom))
        if i[0] > left:
            out.append((left, i[1], i[0], i[3]))
        if i[2] < right:
            out.append((i[2], i[1], right, i[3]))
    return out


def visible_owners(screens, windows):
    """Given the screens and the windows front to back as (owner, rect), return
    the owners with a window that is meaningfully uncovered."""
    owners = []
    uncovered = list(screens)
    for owner, rect in windows:
        if not uncovered:
            break
        shown = sum(area(intersect(r, rect)) for r in uncovered)
        if shown >= MIN_VISIBLE_PIXELS and shown >= MIN_VISIBLE_FRACTION * area(rect):
            owners.append(owner)
        uncovered = subtract(uncovered, rect)
    return owners
