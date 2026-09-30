#!/usr/bin/env python3
"""Assert that every text/background pair in pdf/style.css meets WCAG 2.1 AA.

    python3 pdf/check_contrast.py

Contrast is easy to break by eye, and a PDF is the one format where nobody can
fix it afterwards with a browser extension -- so the intended contract is stated
here explicitly rather than parsed out of the CSS. A styling change that violates
it fails loudly, in CI, in seconds.

Thresholds (WCAG 2.1): 4.5:1 for normal text, 3:1 for large text (>=18pt, or
>=14pt bold). Exits non-zero on any failure.
"""

import sys

# ---------------------------------------------------------------------------
# Palette. Every value here also appears in pdf/style.css as a custom property;
# these are the two places that must agree.
# ---------------------------------------------------------------------------

INK = "#16202B"         # body text
SLATE = "#4A5567"       # secondary text: standfirst, captions, deep contents
BLUE = "#2159CC"        # links, headings, tab labels
BLUE_DEEP = "#103A8C"   # part titles, step labels
GREEN = "#1B6E45"       # Tip label
AMBER = "#8A5A00"       # Warning label
RED = "#B3261E"         # Important label
TEAL = "#0E7C86"        # decorative rules only, never text

WHITE = "#ffffff"
INFO_BG = "#EDF3FE"
SUCCESS_BG = "#E8F5EE"
WARNING_BG = "#FDF3E3"
DANGER_BG = "#FCEDEC"
NEUTRAL_BG = "#F4F6F8"
CODE_BG = "#F5F7FA"
THEAD_BG = "#EDF1F6"

# (description, foreground, background, is_large_text)
PAIRS = [
    # body
    ("body text on white",                INK,       WHITE,      False),
    ("standfirst / caption (slate)",       SLATE,     WHITE,      False),
    ("link (blue) on white",               BLUE,      WHITE,      False),
    ("heading (blue) on white",            BLUE,      WHITE,      True),
    ("part title (deep blue)",             BLUE_DEEP, WHITE,      True),
    ("unlinked dead reference (slate)",    SLATE,     WHITE,      False),

    # code
    ("inline code on code tint",           INK,       CODE_BG,    False),
    ("code block text on tint",            INK,       CODE_BG,    False),
    ("code caption (slate) on white",      SLATE,     WHITE,      False),

    # cover: plain white
    ("cover-blue title",                   INK,   WHITE, True),
    ("cover-blue subtitle",                SLATE, WHITE, True),
    ("cover-blue meta (10pt)",             SLATE, WHITE, False),
    ("cover-blue legal (8.5pt)",           SLATE, WHITE, False),
    ("cover-blue link",                    BLUE,  WHITE, False),

    # cover: light
    ("cover-light title",                  BLUE_DEEP, INFO_BG,    True),
    ("cover-light subtitle",               SLATE,     INFO_BG,    True),
    ("cover-light meta (10pt)",            SLATE,     INFO_BG,    False),
    ("cover-light legal (8.5pt)",          SLATE,     INFO_BG,    False),
    ("cover-light link",                   BLUE,      INFO_BG,    False),

    # contents
    ("contents entry on white",            INK,       WHITE,      False),
    ("contents part entry (deep blue)",    BLUE_DEEP, WHITE,      False),
    ("contents deep entry (slate)",        SLATE,     WHITE,      False),

    # callouts: body text and the bold severity label sit on the tint
    ("Note text on info tint",             INK,       INFO_BG,    False),
    ("Note label (blue) on info tint",     BLUE,      INFO_BG,    False),
    ("Tip text on success tint",           INK,       SUCCESS_BG, False),
    ("Tip label (green) on success tint",  GREEN,     SUCCESS_BG, False),
    ("Warning text on warning tint",       INK,       WARNING_BG, False),
    ("Warning label (amber) on tint",      AMBER,     WARNING_BG, False),
    ("Important text on danger tint",      INK,       DANGER_BG,  False),
    ("Important label (red) on tint",      RED,       DANGER_BG,  False),
    ("link inside a callout (blue)",       BLUE,      INFO_BG,    False),

    # tables, cards, keys
    ("table header on tint",               INK,       THEAD_BG,   False),
    ("table cell on white",                INK,       WHITE,      False),
    ("card title (blue) on neutral tint",  BLUE,      NEUTRAL_BG, False),
    ("card body on neutral tint",          INK,       NEUTRAL_BG, False),
    ("<kbd> text on neutral tint",         INK,       NEUTRAL_BG, False),
    ("page reference link on tint",        BLUE,      NEUTRAL_BG, False),
    ("API reference label on tint",        BLUE_DEEP, NEUTRAL_BG, False),

    # tabs and steps
    ("tab label (blue) on white",          BLUE,      WHITE,      False),
    ("step label (deep blue) on white",    BLUE_DEEP, WHITE,      False),
]

# Decorative fills carry no text, so the text thresholds do not apply -- but they
# must still be discernible against what they sit on, so the ratios are printed.
DECORATIVE = [
    ("teal rule on white",                 TEAL,      WHITE),
    ("blue rule on white",                 BLUE,      WHITE),
    ("callout edge (blue) on info tint",   BLUE,      INFO_BG),
    ("step rule (blue) on white",          BLUE,      WHITE),
]


def _channel(value):
    value /= 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def luminance(hex_color):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(fg, bg):
    a, b = luminance(fg), luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def main():
    failures = []
    print(f"{'pair':38} {'ratio':>6}  {'need':>5}  result")
    print("-" * 64)
    for label, fg, bg, large in PAIRS:
        ratio = contrast(fg, bg)
        need = 3.0 if large else 4.5
        ok = ratio >= need
        grade = "AAA" if ratio >= (4.5 if large else 7) else ("AA" if ok else "FAIL")
        print(f"{label:38} {ratio:6.2f}  {need:5.1f}  {grade}")
        if not ok:
            failures.append((label, fg, bg, ratio, need))

    print("\ndecorative (no text, informational only):")
    for label, fg, bg in DECORATIVE:
        print(f"  {label:36} {contrast(fg, bg):6.2f}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} pair(s) below WCAG 2.1 AA")
        for label, fg, bg, ratio, need in failures:
            print(f"  {label}: {fg} on {bg} = {ratio:.2f}, needs {need:.1f}")
        return 1
    print(f"PASS: all {len(PAIRS)} text pairs meet WCAG 2.1 AA")
    return 0


if __name__ == "__main__":
    sys.exit(main())
