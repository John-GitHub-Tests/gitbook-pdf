"""Convert GitBook-flavored Markdown into plain Markdown suitable for PDF.

GitBook's custom blocks (`{% hint %}`, `{% tabs %}`, `{% stepper %}`, ...) mean
nothing to Pandoc, which passes them through as literal body text -- without
this module readers see `{% hint style="info" %}` in the prose. This file owns
that rewriting plus link and asset resolution; `build.py` owns page ordering and
the PDF pipeline.

Three properties of the corpus drive the design:

* **Blocks span code fences.** A `{% stepper %}` opens, several fenced samples
  follow, then `{% endstepper %}` closes. Pairing an open/close marker with a
  single `re.DOTALL` regex therefore fails on real pages, so block conversion is
  one line-oriented pass that carries fence state.
* **Markers are not always alone on a line.** `{% content-ref url="./" %} .
  {% endcontent-ref %}` occurs verbatim, so replacements inject their own line
  breaks instead of assuming they own the line.
* **Anything inside a fence is content, not markup.** These docs *document*
  GitBook syntax, so `{% hint %}` inside a fenced example must survive
  untouched. Every transform here is fence-aware for that reason.

The full tag inventory is in `pdf/README.md`.
"""

import html as _html
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from urllib.parse import quote, unquote

# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?",
                            re.DOTALL)
KEY_RE = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$")
BLOCK_SCALAR = (">", ">-", ">+", "|", "|-", "|+")


def split_frontmatter(text):
    """Return `(metadata, body)` for one page.

    Only top-level keys are read, and only the shapes GitBook actually writes:
    `key: value`, and a folded scalar (`description: >-`) continuing on indented
    lines. A bare `key:` introducing a nested mapping -- `metaLinks:`, on most
    GitBook pages -- has its block consumed by the same indentation rule, so its
    children are never mistaken for top-level keys. That is the whole reason
    this is hand-rolled rather than a `line.split(":")`: PyYAML would do it
    properly but is not in the standard library, and a naive split turns
    `description: >-` into the literal value ">-".
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text

    meta = {}
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        km = KEY_RE.match(lines[i])
        i += 1
        if not km:
            continue
        key, value = km.group(1), km.group(2).strip()
        if value and value not in BLOCK_SCALAR:
            meta[key] = value.strip("'\"")
            continue
        # Either a block scalar or a nested mapping: consume the indented run.
        block = []
        while i < len(lines) and (not lines[i].strip()
                                  or lines[i][:1] in " \t"):
            block.append(lines[i].strip())
            i += 1
        # Folded and literal scalars both collapse to one line here; these
        # values are used as a single-paragraph standfirst, not as prose.
        meta[key] = " ".join(p for p in block if p) if value else ""
    return meta, text[m.end():]


# ---------------------------------------------------------------------------
# Fence tracking
# ---------------------------------------------------------------------------

# A fence nested in a list item opens indented past the usual 0-3 spaces --
# by however wide that item's own marker is -- so an *opening* fence also
# accepts one list-marker prefix ahead of the indent. A closing fence gets no
# such prefix (it is a bare continuation line of the item), so it is matched
# by `FENCE_CLOSE_RE` instead: unbounded leading whitespace, nothing but the
# marker run. Once already inside a fence, a bare marker run at any
# indentation is unambiguous enough that being lenient here is safe -- prose
# does not consist of just backticks.
FENCE_RE = re.compile(
    r"^(?:[-*+]\s+|\d+[.)]\s+)?(\s{0,3})(`{3,}|~{3,})(.*)$")
FENCE_CLOSE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")


def iter_lines(text):
    """Yield `(line, in_code)` for every line, `in_code` covering the fences.

    Callers pass fenced lines through verbatim; see the module docstring for
    why that matters more here than in most docs corpora.
    """
    fence = None  # (marker_char, marker_len)
    for line in text.split("\n"):
        if fence is None:
            m = FENCE_RE.match(line)
            fence = (m.group(2)[0], len(m.group(2))) if m else None
            yield line, m is not None
        else:
            yield line, True
            m = FENCE_CLOSE_RE.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
                fence = None


def map_prose_lines(text, fn):
    """Rewrite non-code lines with `fn`, which may return multi-line text."""
    return "\n".join(line if in_code else fn(line)
                     for line, in_code in iter_lines(text))


def prose_only(fn):
    """Wrap a line-local transform so fenced code is left verbatim.

    Only for transforms that are line-local: anything pairing an opening and
    closing marker has to go through `convert_blocks` instead.
    """
    def wrapped(text, *a, **kw):
        return map_prose_lines(text, lambda line: fn(line, *a, **kw))
    return wrapped


INLINE_CODE_RE = re.compile(r"`+[^`]*`+")


def outside_inline_code(line, fn):
    """Apply `fn` only to the parts of `line` outside a backtick span."""
    out, pos = [], 0
    for m in INLINE_CODE_RE.finditer(line):
        out.append(fn(line[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fn(line[pos:]))
    return "".join(out)


# ---------------------------------------------------------------------------
# Markdown escapes
# ---------------------------------------------------------------------------

MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!<>|~])")


def unescape_md(text):
    """Strip Markdown backslash escapes, for text reused as display text."""
    return MD_ESCAPE_RE.sub(r"\1", text).strip()


def _esc_inline(s):
    """Make a block attribute value safe to drop into Markdown prose."""
    return _html.escape(s.strip(), quote=False)


# ---------------------------------------------------------------------------
# Marker patterns
# ---------------------------------------------------------------------------

# `.*?%\}` rather than `\s*%\}`: GitBook adds attributes after the style
# (`icon="sparkles"`), and a hint that fails to match here is not reported --
# the catch-all below strips the marker and the callout silently disappears.
HINT_OPEN = re.compile(r"\{%\s*hint\s+style=[\"'](\w+)[\"'].*?%\}")
HINT_CLOSE = re.compile(r"\{%\s*endhint\s*%\}")
TABS_OPEN = re.compile(r"\{%\s*tabs\s*%\}")
TABS_CLOSE = re.compile(r"\{%\s*endtabs\s*%\}")
TAB_OPEN = re.compile(r"\{%\s*tab\s+title=[\"'](.*?)[\"']\s*%\}")
TAB_CLOSE = re.compile(r"\{%\s*endtab\s*%\}")
STEPPER_OPEN = re.compile(r"\{%\s*stepper\s*%\}")
STEPPER_CLOSE = re.compile(r"\{%\s*endstepper\s*%\}")
STEP_OPEN = re.compile(r"\{%\s*step\s*%\}")
STEP_CLOSE = re.compile(r"\{%\s*endstep\s*%\}")
# Attribute values contain literal `%` -- `{% column width="50%" %}` is the
# common case -- so every pattern that spans attributes closes on a non-greedy
# `.*?%\}` rather than `[^%]*%\}`. With `[^%]*` the column markers survive the
# pass and print as body text. Matching is per line, so `.*?` cannot run away.
COLUMN_ANY = re.compile(r"\{%\s*(?:end)?columns?\b.*?%\}")
CODE_OPEN = re.compile(r"\{%\s*code\b(.*?)%\}")
CODE_CLOSE = re.compile(r"\{%\s*endcode\s*%\}")
TITLE_ATTR = re.compile(r"\btitle=[\"'](.*?)[\"']")
CREF_OPEN = re.compile(r"\{%\s*content-ref\s+url=[\"'](.*?)[\"'].*?%\}")
CREF_CLOSE = re.compile(r"\{%\s*endcontent-ref\s*%\}")
EMBED_OPEN = re.compile(r"\{%\s*embed\s+url=[\"'](.*?)[\"'].*?%\}")
FILE_OPEN = re.compile(r"\{%\s*file\s+src=[\"'](.*?)[\"'].*?%\}")
OPENAPI_OPEN = re.compile(r"\{%\s*openapi(-schemas)?\b(.*?)%\}")
OPENAPI_CLOSE = re.compile(r"\{%\s*endopenapi(?:-schemas)?\s*%\}")
MD_LINK_INLINE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")

# Integration blocks (`{% @mailchimp/form %}`) have no print equivalent.
INTEGRATION = re.compile(r"\{%\s*@[\w/.@-]+.*?%\}")
# Adaptive-content conditionals and the "updates" wrapper: keep the body, drop
# the wrapper. Both branches of an `{% if %}` therefore print, the same way both
# panels of a `{% tabs %}` do -- see "Known limitations" in pdf/README.md.
WRAPPER_ONLY = re.compile(r"\{%\s*(?:end)?(?:if|else|updates?|update)\b.*?%\}")
# Backstop for anything the specific rules above missed. What it catches is
# counted in `unrecognized_markers` (by block name), because a marker only this
# removes is usually a block whose formatting was lost, not one that was
# handled -- and without the count nothing says so.
ANY_MARKER = re.compile(r"\{%.*?%\}")
MARKER_NAME_RE = re.compile(r"\{%\s*([\w@/.-]+)")
unrecognized_markers = Counter()

# A few pages escape the braces (`\{% tabs %\}`), which also stops GitBook
# rendering the block on the live site. Normalizing keeps the PDF right; the
# source still wants fixing (`grep -rn '\\{%' docs`).
ESCAPED_MARKER = re.compile(r"\\(\{%)|(%)\\(\})")

# Hint severity carries a word as well as a color, so the distinction survives
# greyscale printing and color-vision deficiency.
HINT_LABEL = {
    "info": "Note",
    "success": "Tip",
    "warning": "Warning",
    "danger": "Important",
}

OPENAPI_ATTRS = re.compile(r"\b(path|method|schemas|spec|src)=[\"'](.*?)[\"']")

# Authors often open a hint's body with their own bold label, because on
# the site the callout shows no label of its own. Injecting one as well prints
# "Note: Note:", so the author's is kept and the injected one collapses into it.
DUP_HINT_LABEL = re.compile(
    r"(\*\*(Note|Tip|Warning|Important):\*\*)\n+\*\*\2:?\*\*[ \t]*")


def unescape_markers(text):
    return map_prose_lines(
        text, lambda ln: ESCAPED_MARKER.sub(
            lambda m: m.group(1) or (m.group(2) + m.group(3)), ln))


# ---------------------------------------------------------------------------
# Page titles, for reference labels
# ---------------------------------------------------------------------------

H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.M)
_title_cache = {}


def page_title_from_file(abs_path):
    """First H1 of a page, or None. Cached: pages are referenced repeatedly."""
    if abs_path not in _title_cache:
        try:
            with open(abs_path, encoding="utf-8") as fh:
                _, body = split_frontmatter(fh.read())
        except OSError:
            body = ""
        m = H1_RE.search(body)
        _title_cache[abs_path] = unescape_md(m.group(1)) if m else None
    return _title_cache[abs_path]


def make_ref_label(page_path, space_dir, titles):
    """Build a resolver mapping a `{% content-ref %}` URL to the target's title.

    GitBook stores the block with the target's *filename* as its link text and
    renders the real title from its own database, so the checkout is full of
    refs labelled `inline.md` -- and `{% content-ref url="./" %}` carries the
    label `.`. Printed verbatim that is the one place a PDF reference reads
    worse than the website.

    `SUMMARY.md` is consulted first so a reference names a page exactly as the
    contents list and the PDF outline do; a page outside the navigation falls
    back to its own H1.
    """
    page_dir = os.path.dirname(page_path)

    def resolve(url):
        if not url or url.startswith(ABSOLUTE):
            return None
        raw = unquote(url.partition("#")[0])
        if not raw:
            return None
        target = os.path.normpath(os.path.join(page_dir, raw))
        rel = os.path.relpath(target, space_dir).replace(os.sep, "/")
        for cand in (rel, f"{rel}/README.md", f"{rel}.md"):
            if cand in titles:
                return titles[cand]
        for cand in (target, os.path.join(target, "README.md"), f"{target}.md"):
            if os.path.isfile(cand):
                return page_title_from_file(cand)
        return None

    return resolve


# ---------------------------------------------------------------------------
# Block conversion (single fence-aware pass)
# ---------------------------------------------------------------------------

def convert_blocks(text, ref_label=None):
    """Rewrite every GitBook block marker into a Pandoc fenced div.

    One pass, because blocks nest and span fences: a stack tracks step
    numbering, a counter tracks open divs so the page can be balanced, and a
    small state flag handles the multi-line `{% content-ref %}` body.
    """
    text = unescape_markers(text)
    out = []
    step_counters = []       # one counter per open {% stepper %}
    cref = None              # (url, [buffered lines]) while inside content-ref
    depth = [0]              # open fenced divs

    def open_div(classes):
        depth[0] += 1
        return f"\n::: {{{classes}}}\n"

    def close_div():
        # A stray close in unbalanced source would otherwise emit a bare ':::'
        # that Pandoc prints literally.
        if depth[0] == 0:
            return ""
        depth[0] -= 1
        return "\n:::\n"

    for line, in_code in iter_lines(text):
        if in_code:
            (cref[1] if cref is not None else out).append(line)
            continue

        # --- content-ref: buffer until the closing marker -------------------
        if cref is not None:
            if CREF_CLOSE.search(line):
                url, body = cref[0], "\n".join(cref[1])
                inner = MD_LINK_INLINE.search(body)
                # The target's real title first: that is what GitBook renders,
                # and it ignores the label stored in the block body.
                label = ref_label(url) if ref_label else None
                if not label and inner and inner.group(1).strip(" .") :
                    label = inner.group(1)
                if not label:
                    label = url.rstrip("/").split("/")[-1] or url
                out.append(f"\n::: {{.pageref}}\n"
                           f"[{_esc_inline(label)}]({url})\n:::\n")
                cref = None
            else:
                cref[1].append(line)
            continue

        m = CREF_OPEN.search(line)
        if m:
            before = line[:m.start()].rstrip()
            if before:
                out.append(before)
            cref = (m.group(1), [line[m.end():]])
            continue

        line = INTEGRATION.sub("", line)

        # --- fenced code with GitBook attributes ---------------------------
        # `overflow` and `lineNumbers` describe on-screen behavior with no print
        # equivalent; only a title survives, as a caption above the block.
        def code_open_sub(mm):
            tm = TITLE_ATTR.search(mm.group(1))
            return (f"\n::: {{.codetitle}}\n`{_esc_inline(tm.group(1))}`\n:::\n"
                    if tm else "")

        line = CODE_OPEN.sub(code_open_sub, line)
        line = CODE_CLOSE.sub("", line)

        # --- hints ---------------------------------------------------------
        def hint_open_sub(mm):
            style = mm.group(1).lower()
            return (open_div(f".hint .hint-{style}")
                    + f"**{HINT_LABEL.get(style, 'Note')}:**\n")

        line = HINT_OPEN.sub(hint_open_sub, line)
        line = HINT_CLOSE.sub(lambda mm: close_div(), line)

        # --- tabs: every panel prints, in order ----------------------------
        line = TABS_OPEN.sub(lambda mm: open_div(".tabs"), line)
        line = TABS_CLOSE.sub(lambda mm: close_div(), line)
        line = TAB_OPEN.sub(
            lambda mm: open_div(".tab")
            + f"**{_esc_inline(mm.group(1))}**\n", line)
        line = TAB_CLOSE.sub(lambda mm: close_div(), line)

        # --- stepper: numbered explicitly, since CSS counters do not survive
        #     the div flattening Pandoc applies -----------------------------
        def stepper_open_sub(mm):
            step_counters.append(0)
            return open_div(".stepper")

        def stepper_close_sub(mm):
            if step_counters:
                step_counters.pop()
            return close_div()

        def step_open_sub(mm):
            if not step_counters:      # a step outside any stepper
                step_counters.append(0)
            step_counters[-1] += 1
            return open_div(".step") + f"**Step {step_counters[-1]}**\n"

        line = STEPPER_OPEN.sub(stepper_open_sub, line)
        line = STEPPER_CLOSE.sub(stepper_close_sub, line)
        line = STEP_OPEN.sub(step_open_sub, line)
        line = STEP_CLOSE.sub(lambda mm: close_div(), line)

        # --- columns: a printed page is one column -------------------------
        line = COLUMN_ANY.sub("", line)

        # --- media and API reference blocks --------------------------------
        line = EMBED_OPEN.sub(
            lambda mm: f"\n::: {{.embed}}\n<{mm.group(1)}>\n:::\n", line)
        line = FILE_OPEN.sub(
            lambda mm: f"\n::: {{.filelink}}\nFile: [{_esc_inline(os.path.basename(mm.group(1).rstrip('/')))}]({mm.group(1)})\n:::\n",
            line)

        # An OpenAPI block is rendered by GitBook from a live specification;
        # there is nothing in the checkout to render, so name the operation and
        # point at the specification rather than dropping it silently.
        def openapi_sub(mm):
            attrs = dict(OPENAPI_ATTRS.findall(mm.group(2)))
            if mm.group(1):     # openapi-schemas
                what = attrs.get("schemas") or attrs.get("spec") or "schema"
                label = f"API schema: `{_esc_inline(what)}`"
            else:
                method = attrs.get("method", "").upper()
                path = attrs.get("path", "")
                what = f"{method} {path}".strip() or "operation"
                label = f"API reference: `{_esc_inline(what)}`"
            src = attrs.get("src", "")
            tail = f"\n\nFrom the OpenAPI specification at <{src}>." if src else ""
            return (f"\n::: {{.apiref}}\n**{label}**{tail}\n\n"
                    f"Rendered interactively in the published documentation.\n:::\n")

        line = OPENAPI_OPEN.sub(openapi_sub, line)
        line = OPENAPI_CLOSE.sub("", line)

        line = WRAPPER_ONLY.sub("", line)
        for leftover in ANY_MARKER.findall(line):
            nm = MARKER_NAME_RE.match(leftover)
            unrecognized_markers[nm.group(1) if nm else leftover] += 1
        line = ANY_MARKER.sub("", line)
        out.append(line)

    if cref is not None:        # unbalanced open at end of page
        out.append(f"\nSee <{cref[0]}>.\n")

    # Left open, a div runs to the end of the *concatenated* document and drags
    # every later page inside it, so close whatever this page left open.
    if depth[0]:
        out.append("\n" + "\n:::\n" * depth[0])

    return DUP_HINT_LABEL.sub(r"\1\n\n", "\n".join(out))


# ---------------------------------------------------------------------------
# Includes
# ---------------------------------------------------------------------------

INCLUDE_RE = re.compile(r"\{%\s*include\s+[\"'](.*?)[\"']\s*%\}")
_missing_includes = set()


def resolve_includes(text, page_path, depth=0):
    """Inline local include files; drop remote and unresolvable ones.

    Includes normally point at `.gitbook/includes/`, which is in the checkout,
    so almost all of them inline. A reusable block living only in
    GitBook (`~/reusable/...`) cannot be fetched from a source checkout and is
    dropped -- reported once per target so a genuinely missing file is visible
    without one warning per page.
    """
    if depth > 3:              # include cycles exist in the wild
        return map_prose_lines(text, lambda ln: INCLUDE_RE.sub("", ln))

    def sub_line(line):
        def sub(m):
            target = m.group(1)
            if target.startswith(("http://", "https://")):
                return ""
            resolved = os.path.normpath(
                os.path.join(os.path.dirname(page_path), target))
            if not os.path.isfile(resolved):
                if target not in _missing_includes:
                    _missing_includes.add(target)
                    print(f"  note: include not in checkout, dropped: {target}",
                          file=sys.stderr)
                return ""
            try:
                with open(resolved, encoding="utf-8") as fh:
                    inner = fh.read()
            except OSError:
                return ""
            _, inner = split_frontmatter(inner)
            inner = resolve_includes(inner, resolved, depth + 1).strip()
            return "\n" + inner + "\n" if inner else ""
        return INCLUDE_RE.sub(sub, line)

    return map_prose_lines(text, sub_line)


# ---------------------------------------------------------------------------
# Card tables
# ---------------------------------------------------------------------------

# `data-view` is not always the first attribute -- GitBook writes
# `<table data-card-size="large" data-view="cards">` -- and a card table this
# misses prints as a raw grid of icon, title and link cells.
CARD_TABLE_RE = re.compile(
    r"<table\b[^>]*\bdata-view=\"cards\"[^>]*>(.*?)</table>", re.S | re.I)
HEADING_TAG_RE = re.compile(r"</?h[1-6]\b[^>]*>", re.I)
ANY_TAG_RE = re.compile(r"<[^>]+>")
TH_RE = re.compile(r"<th\b([^>]*)>(.*?)</th>", re.S | re.I)
TD_RE = re.compile(r"<td\b([^>]*)>(.*?)</td>", re.S | re.I)
TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
THEAD_RE = re.compile(r"<thead\b[^>]*>(.*?)</thead>", re.S | re.I)
TBODY_RE = re.compile(r"<tbody\b[^>]*>(.*?)</tbody>", re.S | re.I)
HREF_IN_RE = re.compile(r"href=\"([^\"]*)\"", re.I)


def convert_cards(text):
    """Turn GitBook card tables into a linear list of cards.

    A card table is a normal `<table>` whose columns are mostly `data-hidden`:
    the visible ones hold the card's title and description, while hidden ones
    carry the cover images and a `data-card-target` content-ref. Printed as a
    table it is unreadable -- the reader gets a row of image filenames and a
    column of empty cells, which is what a docs front page would otherwise
    look like.

    So the hidden columns are dropped, except `data-card-target`, whose URL
    becomes the card title's link. Cover images are dropped on purpose: they are
    decorative screenshots that add megabytes and no information.

    Cells are cleaned before use: heading tags are removed (a card's `<h4>`
    would otherwise become a PDF bookmark), and a cell holding only an icon is
    folded into the title rather than taking the title's place.
    """
    def is_icon_only(cell):
        return (FA_ICON_RE.search(cell) is not None
                and not ANY_TAG_RE.sub("", FA_ICON_RE.sub("", cell)).strip())

    def convert(m):
        inner = m.group(1)
        thead = THEAD_RE.search(inner)
        headers = TH_RE.findall(thead.group(1)) if thead else []
        hidden = {i for i, (attrs, _) in enumerate(headers)
                  if "data-hidden" in attrs.lower()}
        target_col = next((i for i, (attrs, _) in enumerate(headers)
                           if "data-card-target" in attrs.lower()), None)

        body = TBODY_RE.search(inner)
        cards = []
        for row in TR_RE.findall(body.group(1) if body else inner):
            cells = TD_RE.findall(row)
            if not cells:
                continue
            href = None
            if target_col is not None and target_col < len(cells):
                hm = HREF_IN_RE.search(cells[target_col][1])
                href = hm.group(1) if hm else None
            visible = [HEADING_TAG_RE.sub("", c[1]).strip()
                       for i, c in enumerate(cells) if i not in hidden]
            icon_cells = [c for c in visible if is_icon_only(c)]
            text_cells = [c for c in visible
                          if c and not is_icon_only(c)]
            if not text_cells:
                continue
            title = " ".join(icon_cells + [text_cells[0]])
            rest = "".join(f"<div>{c}</div>" for c in text_cells[1:])
            if href:
                title = f'<a href="{_html.escape(href, quote=True)}">{title}</a>'
            cards.append(f'<li class="card"><p class="card-title">{title}</p>'
                         f'{rest}</li>')
        if not cards:
            return ""
        return '<ul class="cards">' + "".join(cards) + "</ul>"

    return CARD_TABLE_RE.sub(convert, text)


# ---------------------------------------------------------------------------
# Raw HTML normalization
# ---------------------------------------------------------------------------

PICTURE_RE = re.compile(r"<picture\b[^>]*>(.*?)</picture>", re.S | re.I)
IMG_IN_RE = re.compile(r"<img\b[^>]*>", re.S | re.I)
SRCSET_RE = re.compile(r"\s+srcset=\"[^\"]*\"", re.I)
DETAILS_RE = re.compile(r"<details(?![^>]*\bopen\b)([^>]*)>", re.I)
# GitBook's own icon markup, e.g. `<i class="fa-book-open">:book-open:</i>`
# -- a Font Awesome icon name plus a `:name:` placeholder as the tag's text
# content. On the published site this becomes an inline SVG (Font Awesome's
# own path data, `viewBox`, `fill="currentColor"`); this reproduces that
# rather than the older assumption that these tags render nothing without an
# embedded webfont. `.*?` (not just an empty tag) covers the placeholder text.
FA_ICON_RE = re.compile(r'<i\s+class="fa-([a-z0-9-]+)"[^>]*>.*?</i>',
                        re.I | re.S)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FA_ICON_CACHE_DIR = os.path.join(SCRIPT_DIR, "vendor", "fontawesome")
# Font Awesome Free ships ~2000 icons; GitBook's own picker draws from a
# larger Pro set, so some names (e.g. multi-word ones) simply are not in the
# free set and never will be -- not a fetch failure, a real gap.
FA_ICON_BASE_URL = ("https://raw.githubusercontent.com/FortAwesome/"
                    "Font-Awesome/6.x/svgs/solid/")
FA_ICON_SVG_RE = re.compile(r'viewBox="([^"]+)"[^>]*>(.*)</svg>', re.S)


def _resolve_fa_icon(name):
    """Return `(viewBox, inner_svg)` for a Font Awesome Free solid icon.

    Cached to `vendor/fontawesome/<name>.svg` once fetched, or
    `<name>.missing` once confirmed absent (a 404, or no network reached) --
    either way, a name is never looked up twice in one build, and a rerun
    reuses what a prior one already resolved. Returns None, silently, for a
    repeat cache hit on an already-known-missing icon; the one-time warning
    fires only where the miss is first discovered, in `_fa_icon_sub`.
    """
    os.makedirs(FA_ICON_CACHE_DIR, exist_ok=True)
    cached = os.path.join(FA_ICON_CACHE_DIR, f"{name}.svg")
    missing = os.path.join(FA_ICON_CACHE_DIR, f"{name}.missing")
    if os.path.isfile(missing):
        return None
    if not os.path.isfile(cached):
        try:
            with urllib.request.urlopen(FA_ICON_BASE_URL + name + ".svg",
                                        timeout=5) as resp:
                data = resp.read().decode("utf-8")
        except (urllib.error.URLError, OSError, ValueError):
            open(missing, "w").close()
            return None
        with open(cached, "w", encoding="utf-8") as fh:
            fh.write(data)
    with open(cached, encoding="utf-8") as fh:
        data = fh.read()
    m = FA_ICON_SVG_RE.search(data)
    if not m:
        open(missing, "w").close()
        return None
    return m.group(1), m.group(2)


_fa_icon_warned = set()


def _fa_icon_sub(m):
    name = m.group(1).lower()
    resolved = _resolve_fa_icon(name)
    if not resolved:
        if name not in _fa_icon_warned:
            _fa_icon_warned.add(name)
            print(f"  warn: Font Awesome icon 'fa-{name}' not in the free "
                  f"set (or no network to fetch it) -- dropped",
                  file=sys.stderr)
        return ""
    viewbox, inner = resolved
    # Sized and colored to sit inline with surrounding text, matching
    # GitBook's own convention (`fill="currentColor"`) rather than a fixed
    # pixel size or color that would clash with the theme around it.
    return (f'<svg viewBox="{viewbox}" fill="currentColor" '
            f'style="width:1em;height:1em;vertical-align:-0.125em" '
            f'aria-hidden="true">{inner}</svg>')


CUSTOM_SPAN_RE = re.compile(
    r"<span\s+(?:data-gb-custom-inline|data-option)[^>]*>(.*?)</span>",
    re.S | re.I)
SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
# GitBook Assistant buttons (`data-action="ask"`) open a chat on the site; on
# paper they are inert boxes with a prompt printed in them.
ASK_BUTTON_RE = re.compile(
    r"<button\b[^>]*\bdata-action=\"ask\"[^>]*>.*?</button>", re.S | re.I)


def normalize_html(text):
    """Reduce GitBook's raw HTML to what a printed page can honor.

    * `<picture>` collapses to its `<img>`. GitBook emits a dark-mode `<source>`
      first, and keeping both risks Chrome resolving the dark variant -- white
      icons on white paper. The `<img>` is always the light fallback.
    * `srcset` is stripped for the same reason plus a practical one: its paths
      are not rewritten to `file://`, so Chrome would fail to load them and fall
      back anyway.
    * `<details>` gains `open`. Chrome prints a closed disclosure collapsed, so
      every expandable block would silently lose its content.
    * Font Awesome `<i>` icons become inline SVG (fetched and cached by
      `_resolve_fa_icon`), matching what GitBook's own published site
      renders. An icon outside the free set, or unreachable with no network,
      is dropped instead -- printing as nothing, like before this existed,
      rather than a missing-glyph box.
    * GitBook's inline `<span>` wrappers (emoji, adaptive-content options) are
      unwrapped; the span text already holds the emoji itself.
    * `<script>` is dropped. None appears outside a fenced example today, but
      this HTML is fed to a real browser, so raw script must not be one commit
      away from executing.
    """
    def one_picture(m):
        img = IMG_IN_RE.search(m.group(1))
        return img.group(0) if img else ""

    text = PICTURE_RE.sub(one_picture, text)
    text = SRCSET_RE.sub("", text)
    text = DETAILS_RE.sub(r"<details open\1>", text)
    text = FA_ICON_RE.sub(_fa_icon_sub, text)
    text = CUSTOM_SPAN_RE.sub(r"\1", text)
    text = SCRIPT_RE.sub("", text)
    text = ASK_BUTTON_RE.sub("", text)
    return text


def normalize_html_prose(text):
    """`normalize_html`, applied only outside fenced code.

    These pages document GitBook's HTML output, so `<picture>` and `<script>`
    appear inside examples as content. The rewrite is block-shaped rather than
    line-shaped, so prose runs are collected and converted as units.
    """
    out, buf = [], []

    def flush():
        if buf:
            out.append(normalize_html("\n".join(buf)))
            buf.clear()

    for line, in_code in iter_lines(text):
        if in_code:
            flush()
            out.append(line)
        else:
            buf.append(line)
    flush()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------

# Pandoc's TeX math extensions stay off (see PANDOC_FROM in build.py), so
# GitBook's `$$...$$` spans are shown as code rather than typeset. KaTeX in the
# browser would typeset them properly; with six occurrences in the space that
# is not worth a second JS bundle, and code is honest about being source.
MATH_RE = re.compile(r"\$\$(.+?)\$\$", re.S)


@prose_only
def convert_math(line):
    return MATH_RE.sub(
        lambda m: "`" + m.group(1).strip().replace("`", "'") + "`", line)


# ---------------------------------------------------------------------------
# Mermaid
# ---------------------------------------------------------------------------

MERMAID_RE = re.compile(r"^[ \t]*```mermaid[ \t]*\n(.*?)^[ \t]*```[ \t]*$",
                        re.DOTALL | re.MULTILINE)


def convert_mermaid(text):
    """Hand diagram source to the browser, which renders it before printing.

    Needs the bundle from `pdf/fetch-deps.sh`; without it the diagram prints
    as source.
    """
    return MERMAID_RE.sub(
        lambda m: '\n<pre class="mermaid">'
                  + _html.escape(m.group(1).rstrip()) + "</pre>\n", text)


# ---------------------------------------------------------------------------
# Footnotes
# ---------------------------------------------------------------------------

FOOTNOTE_LABEL = re.compile(r"\[\^([^\]\s]+)\]")


def namespace_footnotes(text, page_key):
    """Prefix footnote labels with a per-page key.

    Pages number footnotes from 1 independently, so concatenating a space
    collides them: Pandoc reports duplicate references and readers get another
    page's footnote text.
    """
    return map_prose_lines(
        text, lambda ln: FOOTNOTE_LABEL.sub(rf"[^{page_key}-\1]", ln))


# ---------------------------------------------------------------------------
# Heading demotion
# ---------------------------------------------------------------------------

ATX_RE = re.compile(r"^(#{1,6})(\s+)(.*)$")


@prose_only
def demote_headings(line, offset):
    """Shift a page's headings to nest under its position in the navigation."""
    if offset <= 0:
        return line
    m = ATX_RE.match(line)
    if not m:
        return line
    return "#" * min(len(m.group(1)) + offset, 6) + m.group(2) + m.group(3)


# ---------------------------------------------------------------------------
# Links and assets
# ---------------------------------------------------------------------------

# The URL body matches an escaped pair (`\)`, `\(`, ...) as one unit ahead of
# an ordinary character, so a destination with a backslash-escaped paren --
# GitBook's own convention for a literal paren in a filename -- doesn't end
# the match at the escaped `)` and truncate the captured URL.
# The URL body treats three things as one unit ahead of an ordinary
# character: an escaped pair (`\)`, `\(`, ...) -- GitBook's convention for a
# literal paren in a filename in most contexts -- and a balanced, unescaped
# `(...)` pair, since GitBook does not escape parens in every context (a
# filename like `setup-(beta)-guide.md` reaches here as literal parens).
# CommonMark itself allows both forms in a bare link destination.
_LINK_URL = r"(?:\\.|\([^()]*\)|[^()>\s])+?"
# The label has the same blind spot the URL did: a literal `\]` in the label
# text (e.g. a Windows registry path used as link text) is two characters,
# not an unescaped closer, so it needs the same escaped-pair treatment.
_LINK_LABEL = r"(?:\\.|[^\]])*"
MD_LINK_RE = re.compile(
    r"(?<!!)\[(" + _LINK_LABEL + r")\]\(<?(" + _LINK_URL
    + r")>?(?:\s+\"[^\"]*\")?\)")
IMG_LINK_RE = re.compile(
    r"!\[(" + _LINK_LABEL + r")\]\(<?(" + _LINK_URL
    + r")>?(?:\s+\"[^\"]*\")?\)")
HTML_HREF_RE = re.compile(r'href="([^"]+)"')
HTML_A_TEXT_RE = re.compile(
    r'(<a\b[^>]*\bhref="([^"]+)"[^>]*>)([^<]*)(</a>)', re.I)
HTML_SRC_RE = re.compile(r'src="([^"]+)"')
ABSOLUTE = ("http://", "https://", "mailto:", "data:", "#", "tel:", "file://")

# A published URL carries no source suffix, and a directory's README.md
# publishes as the directory itself.
PUBLISHED_SUFFIX_RE = re.compile(r"(?:(?:^|/)README)?\.md$", re.I)
PARENT_PREFIX_RE = re.compile(r"^(?:\.\./)+")

# GitBook writes a dead internal link as `/broken/pages/<id>` or
# `/broken/files/<id>`. That is not a URL: it resolves to nothing on the site
# and to nothing on disk. Keeping the link text and dropping the destination is
# the honest treatment -- a reader who clicks would otherwise land nowhere and
# assume the PDF is broken rather than the source.
BROKEN_RE = re.compile(r"^/broken/(?:pages|files)/", re.I)

# A literal `file://` URL in authored content is never a real asset link --
# those are generated internally by asset_target, not written by an author
# -- so it is always a leftover from whatever system predates this GitBook
# space: an internal viewer URL (`file:///document/preview/<id>`), or a page
# reference that has since moved (`file:///installing-the-agent.html`),
# or one to a page in a different product's docs this space doesn't have.
# Only the basename is a usable identifier -- the rest of the "path" is that
# other system's own structure, meaningless here -- so the same
# basename_anchors lookup used for a stale relative link applies. Where it
# doesn't resolve, this is what makes that the same honest unlink treatment
# as a `/broken/` reference, not a dead file:// left to reach the PDF.

# A leftover from whatever system predates this GitBook space: some pages
# link to each other with a bare `UUID-<hex>.html` href instead of a normal
# relative Markdown path, with no reverse mapping in the exported Markdown to
# say which page that file corresponds to. Each page does carry its own
# legacy ID, though -- as the UUID prefixing the fragment of every in-page
# anchor it links to itself with (`own-filename.md#UUID-<hex>_...`) -- so
# `build_uuid_anchor_map` recovers the mapping from that instead of leaving
# these as dead links.
UUID_HTML_RE = re.compile(r"^(UUID-[0-9a-f-]+)\.html$", re.I)
OWN_UUID_ANCHOR_RE = r"{}#(UUID-[0-9a-f-]+)_"

# Two more dead-end schemes found in this content, neither with a
# recoverable destination: `urn:resource:component:<id>` is GitBook's own
# broken-reference marker (these already carry a "???" link label in the
# source -- GitBook could not resolve them either), and `http:/%20https:/...`
# is a source typo (`http:/ https://...`, the space between two mistakenly
# doubled URLs, exported as a literal `%20`) rather than anything this tool
# introduced. The link label already shows the intended URL as plain text in
# every instance found, so unlinking -- not guessing a repair -- is the
# honest treatment, same as a `/broken/` reference.
# `urn:` only means anything as the whole scheme; the malformed-http form is
# searched anywhere in the URL, since GitBook's own editor sometimes prepends
# a bogus page-relative prefix to it (having failed to recognize the doubled
# URL as absolute) rather than leaving it bare.
URN_RE = re.compile(r"^urn:", re.I)
MALFORMED_HTTP_RE = re.compile(r"https?:/%20", re.I)


def build_uuid_anchor_map(space_dir, anchors):
    """Return `{uuid: anchor}` for legacy `UUID-<hex>.html` links.

    A UUID claimed by more than one page (a handful exist -- content that was
    merged or duplicated across a migration) is dropped rather than guessed
    at: ambiguous is worse than left unresolved.
    """
    uuid_to_paths = defaultdict(set)
    for rel_path in anchors:
        try:
            with open(os.path.join(space_dir, rel_path),
                     encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        pat = re.compile(OWN_UUID_ANCHOR_RE.format(
            re.escape(os.path.basename(rel_path))))
        for uuid in set(pat.findall(text)):
            uuid_to_paths[uuid].add(rel_path)
    return {uuid: anchors[paths.pop()]
            for uuid, paths in uuid_to_paths.items() if len(paths) == 1}


def build_basename_anchor_map(anchors):
    """Return `{filename: anchor}` for pages whose basename is unique.

    A relative link that survives a page move without being updated points
    at the old directory but still names the right file -- this is the
    last-resort match for that, tried only after the literal relative path
    fails to resolve. A basename shared by more than one page (`README.md`
    above all, but a few topic files too) is dropped rather than guessed at,
    same reasoning as `build_uuid_anchor_map`.
    """
    by_base = defaultdict(list)
    for rel_path in anchors:
        by_base[os.path.basename(rel_path)].append(rel_path)
    return {base: anchors[paths[0]]
            for base, paths in by_base.items() if len(paths) == 1}


# Editor URLs. GitBook resolves `app.gitbook.com/s/<id>/<path>`
# to the published site when it renders; nothing else does, so a PDF built from
# source has to reverse the mapping or the reader is sent to an editor that
# demands a login. Only IDs in `spaceIds` are mapped: an unknown ID is left alone
# rather than guessed at, since inventing a destination is worse than an
# honest external link.
GITBOOK_SPACE_URL_RE = re.compile(
    r"^https?://app\.gitbook\.com/(?:o/[A-Za-z0-9_-]+/)?s/([A-Za-z0-9_-]+)([^#?]*)",
    re.I)


def parse_space_url(url, space_ids):
    """Split an editor URL into `(space, path, fragment)`, or None."""
    m = GITBOOK_SPACE_URL_RE.match(url)
    if not m:
        return None
    space = space_ids.get(m.group(1))
    if space is None:
        return None
    path = PUBLISHED_SUFFIX_RE.sub("", m.group(2).strip("/"))
    # `~/changes/...` and `~/reusable/...` address GitBook internals with no
    # public URL at all, so the space root is the closest honest target.
    if path.startswith("~"):
        path = ""
    frag = url.partition("#")[2]
    return space, path, (f"#{frag}" if frag else "")


def rewrite_links(text, page_path, space_dir, anchors, web_base, space_ids,
                  space, uuid_anchors=None, basename_anchors=None,
                  ref_label=None):
    """Point in-space links at PDF anchors and everything else at the website.

    Without this, concatenating pages leaves every `.md` link dead. Images
    become absolute `file://` URLs so Chrome can load them out of the checkout.

    `ref_label` (from `make_ref_label`) also fixes link *text*: GitBook stores
    a page mention, and a content-ref cell in a card table, with the target's
    filename as its text (`[permissions.md](...)`) and shows the page title on
    the site instead. A link whose text is exactly that filename gets the title.
    """
    page_dir = os.path.dirname(page_path)
    uuid_anchors = uuid_anchors or {}
    basename_anchors = basename_anchors or {}

    def title_for(label, url):
        """The target's title when `label` is just its filename, else None."""
        if ref_label is None:
            return None
        raw = unescape_md(unquote(url.partition("#")[0])).rstrip("/")
        text = unescape_md(_html.unescape(label)).strip()
        if not raw or text not in (os.path.basename(raw), "."):
            return None
        return ref_label(url)

    def dead_uuid_link(url):
        """True for a `UUID-<hex>.html` href this space can't resolve."""
        raw = unescape_md(url.partition("#")[0])
        m = UUID_HTML_RE.match(os.path.basename(raw))
        return m is not None and m.group(1) not in uuid_anchors

    def dead_file_link(url):
        """True for a literal `file://` href this space can't resolve."""
        if not url.startswith("file://"):
            return False
        raw = unescape_md(url.partition("#")[0])
        return os.path.basename(raw) not in basename_anchors

    def site_url(path, frag=""):
        base = f"{web_base.rstrip('/')}/{path}" if path else web_base
        return base + frag

    def editor_target(url, prefer_anchor=True):
        parsed = parse_space_url(url, space_ids)
        if parsed is None:
            return None
        target_space, path, frag = parsed
        if prefer_anchor and target_space == space and path:
            # A page addressing its own space by absolute URL: prefer the
            # internal anchor so the cross-reference still works offline.
            for cand in (f"{path}.md", f"{path}/README.md"):
                if cand in anchors:
                    return "#" + anchors[cand]
        return site_url(path, frag)

    def anchor_target(url):
        editor = editor_target(url)
        if editor is not None:
            return editor
        # A literal file:// href is never a generated asset link -- see
        # dead_file_link -- so it gets one resolution attempt by basename
        # before being treated as truly external like the other absolute
        # schemes below it.
        if url.startswith("file://"):
            raw = unescape_md(url.partition("#")[0])
            anchor = basename_anchors.get(os.path.basename(raw))
            return f"#{anchor}" if anchor else None
        if url.startswith(ABSOLUTE):
            return None
        raw, _, frag = url.partition("#")
        if not raw:
            return None
        # A path, like a title, can carry Markdown backslash escapes (GitBook
        # writes a literal paren in a filename as `\(`/`\)`).
        raw = unescape_md(raw)
        # A UUID-style href sometimes carries a (usually stale) directory
        # prefix ahead of it; only the filename identifies the target page.
        m = UUID_HTML_RE.match(os.path.basename(raw))
        if m:
            anchor = uuid_anchors.get(m.group(1))
            return f"#{anchor}" if anchor else None
        rel = os.path.relpath(
            os.path.normpath(os.path.join(page_dir, raw)), space_dir)
        rel = rel.replace(os.sep, "/")
        # A directory-style link resolves to that directory's README.
        for cand in (rel, f"{rel}/README.md", f"{rel}.md"):
            if cand in anchors:
                return "#" + anchors[cand]
        # Last resort: the page this pointed at moved since the link was
        # written, but its filename -- unlike its directory -- is unchanged
        # and unique in this space.
        base = basename_anchors.get(os.path.basename(rel))
        if base:
            return "#" + base
        if rel.startswith(".."):
            # Outside the space: send the reader to the published site. The
            # fragment is kept here, unlike an in-space link, because the target
            # page is not in this PDF and its section anchor still does useful
            # work on the site.
            return site_url(
                PUBLISHED_SUFFIX_RE.sub("", PARENT_PREFIX_RE.sub("", rel)),
                f"#{frag}" if frag else "")
        return None

    def resolve_local(url):
        """Absolute path for a relative URL that exists on disk, else None."""
        if url.startswith(ABSOLUTE):
            return None
        # Asset paths are percent-encoded in the source while the filesystem
        # holds the decoded name, so decode before the existence check --
        # and Markdown-unescape too, for the same reason as `anchor_target`.
        raw = unescape_md(unquote(url.partition("#")[0]))
        if not raw:
            return None
        resolved = os.path.normpath(os.path.join(page_dir, raw))
        return resolved if os.path.isfile(resolved) else None

    def asset_target(url):
        resolved = resolve_local(url)
        return ("file://" + quote(os.path.abspath(resolved))) if resolved \
            else None

    def is_asset_link(url):
        """True for a link (not an image) pointing at a non-page local file.

        GitBook links an inserted image to the asset itself, and `{% file %}`
        links the attachment. Neither destination exists for a PDF reader: the
        relative path resolves against wherever the file happens to sit, so it
        is a link that silently goes nowhere. The image or filename is kept and
        the destination dropped -- the same trade as a `/broken/` reference.
        """
        resolved = resolve_local(url)
        return resolved is not None and not resolved.lower().endswith(".md")

    def fix_line(line):
        def img_sub(m):
            new = asset_target(m.group(2))
            return f"![{m.group(1)}]({new})" if new else m.group(0)

        def src_sub(m):
            new = asset_target(m.group(1))
            return f'src="{new}"' if new else m.group(0)

        def md_sub(m):
            label, url = m.group(1), m.group(2)
            if BROKEN_RE.match(url) or URN_RE.match(url) \
                    or MALFORMED_HTTP_RE.search(url) or dead_uuid_link(url) \
                    or dead_file_link(url):
                return label
            new = anchor_target(url)
            if new:
                better = title_for(label, url)
                if better:
                    label = re.sub(r"([\\\[\]])", r"\\\1", better)
                return f"[{label}]({new})"
            return label if is_asset_link(url) else m.group(0)

        def a_text_sub(m):
            better = title_for(m.group(3), m.group(2))
            if not better:
                return m.group(0)
            return m.group(1) + _html.escape(better, quote=False) + m.group(4)

        def href_sub(m):
            url = m.group(1)
            if BROKEN_RE.match(url) or URN_RE.match(url) \
                    or MALFORMED_HTTP_RE.search(url) or dead_uuid_link(url) \
                    or dead_file_link(url):
                return 'class="dead-link"'
            new = anchor_target(url)
            if new:
                return f'href="{new}"'
            # Dropping the attribute leaves an inert `<a>`, which keeps the
            # wrapped image or text intact without inventing a destination.
            return "" if is_asset_link(url) else m.group(0)

        line = IMG_LINK_RE.sub(img_sub, line)
        line = HTML_SRC_RE.sub(src_sub, line)
        line = MD_LINK_RE.sub(md_sub, line)
        # Before href rewriting, while the href is still the source path.
        line = HTML_A_TEXT_RE.sub(a_text_sub, line)
        line = HTML_HREF_RE.sub(href_sub, line)
        return line

    return map_prose_lines(text, fix_line)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _first_heading_matches(body, title):
    """True when the page opens with a heading that just repeats its TOC title."""
    m = re.search(r"^#{1,6}\s+(.*)$", body.lstrip()[:400], re.MULTILINE)
    if not m:
        return False
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())
    return norm(m.group(1)) == norm(title)


def preprocess(text, *, page_path, space_dir, anchors, heading_offset,
               web_base, anchor_id, title, titles=None, space="",
               space_ids=None, uuid_anchors=None, basename_anchors=None):
    """Turn one GitBook page into PDF-ready Markdown."""
    meta, body = split_frontmatter(text)

    body = resolve_includes(body, page_path)
    ref_label = make_ref_label(page_path, space_dir, titles or {})
    body = convert_blocks(body, ref_label=ref_label)
    body = namespace_footnotes(body, anchor_id)
    # Cards before link rewriting, so a card's `data-card-target` URL is
    # rewritten like any other link; HTML normalization before it too, so only
    # the surviving `<img src>` needs resolving.
    body = convert_cards(body)
    body = normalize_html_prose(body)
    body = convert_math(body)
    body = convert_mermaid(body)
    body = rewrite_links(body, page_path, space_dir, anchors, web_base,
                         space_ids or {}, space, uuid_anchors,
                         basename_anchors, ref_label)

    # Drop the page's own leading H1 when it merely repeats the navigation
    # title -- before demotion, so the comparison sees the original level.
    if _first_heading_matches(body, title):
        body = re.sub(r"\A\s*#{1,6}[ \t]+.*?(?:\r?\n|\Z)", "", body, count=1)

    body = demote_headings(body, heading_offset)

    # Every page gets a heading at its navigation depth, so the PDF outline
    # mirrors SUMMARY.md even where a page's own H1 is missing or differs.
    level = min(heading_offset + 1, 6)
    head = f'{"#" * level} {title} {{#{anchor_id}}}\n\n'

    # GitBook shows the frontmatter description under the page title, and it is
    # often the only sentence saying what the page is for.
    # Unescaped first: descriptions carry Markdown escapes (`—\_perfect`) that
    # would print as backslashes, since this is emitted as raw HTML.
    desc = unescape_md(meta.get("description") or "")
    if desc:
        head += f'<p class="standfirst">{_html.escape(desc)}</p>\n\n'

    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return head + body + "\n"


if __name__ == "__main__":
    # Smoke-test one page: python3 pdf/gitbook_preprocess.py <file.md>
    path = sys.argv[1]
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    print(preprocess(
        src, page_path=path, space_dir=os.path.dirname(path) or ".",
        anchors={}, heading_offset=0, web_base="https://example.com/docs",
        anchor_id="smoke", title="Smoke Test"))
