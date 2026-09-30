#!/usr/bin/env python3
"""Build a PDF of a GitBook space from the Markdown in a repository.

    python3 pdf/build.py docs                 # -> dist/gitbook-docs.pdf
    python3 pdf/build.py --all
    python3 pdf/build.py docs --limit 20 --keep-html   # fast iteration

GitBook's own PDF export is capped at 100 pages per download, so whole-space
output has to be generated from source.

Reading order and heading hierarchy come from the space's `SUMMARY.md`, which is
GitBook's own navigation tree: alphabetical concatenation would scramble the
order, and the `##` part headings in it are the top level of the contents.

Pipeline: SUMMARY.md -> manifest -> preprocess -> Pandoc (md->html) -> headless
Chrome --print-to-pdf -> qpdf. Chrome rather than LaTeX because these pages
carry raw HTML tables, `<details>` blocks and card grids that a TeX engine
cannot render, and because it lets `pdf/style.css` own the layout.

This tool is meant to be reusable across repositories -- as a plain checkout or
via `action.yml` as a GitHub Action -- so two directories are kept distinct:
`SCRIPT_DIR` (where this tool's own assets live: `style.css`, the Mermaid
vendor bundle) and `REPO_ROOT` (the docs to build, which is wherever the
caller's working directory is -- the consuming repository's checkout when run
as an action, or the current checkout when run in place). Site-specific values
(publish URL, per-space titles, cross-space editor-link IDs) are not hardcoded
here; see `load_config`.
"""

import argparse
import datetime
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from gitbook_preprocess import (  # noqa: E402
    INLINE_CODE_RE, build_basename_anchor_map, build_uuid_anchor_map,
    iter_lines, preprocess, unescape_md, unrecognized_markers)

# The docs to build -- not necessarily the repository this script lives in.
# Overridable with --repo-root; defaults to the caller's working directory so
# that running this as an action against another repo's checkout builds that
# repo's docs, not this tool's own.
REPO_ROOT = os.getcwd()
PDF_DIR = SCRIPT_DIR

DEFAULT_CONFIG_FILE = "pdf.config.json"


def load_config(repo_root, config_path, web_base_override):
    """Return `(web_base, space_titles, space_ids, copyright_holder,
    cover_subtitle)`.

    Site-specific values live in an optional JSON file at the repo being
    built (`pdf.config.json` by default -- see `pdf/README.md`), not in this
    script, so the same tool works unmodified against any GitBook-synced
    checkout. A missing file is not an error: space titles fall back to their
    directory name, cross-space editor links are simply left unmapped, and
    the cover omits both the copyright line and the "authoritative version"
    pointer.

        {
          "webBase": "https://example.com/docs",
          "spaceTitles": {"docs": "Example Documentation"},
          "spaceIds": {"<gitbook-space-id>": "docs"},
          "copyrightHolder": "Example Corp",
          "coverSubtitle": "Administrator guide"
        }
    """
    path = config_path if os.path.isabs(config_path) \
        else os.path.join(repo_root, config_path)
    config = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            try:
                config = json.load(fh)
            except json.JSONDecodeError as exc:
                sys.exit(f"error: {path} is not valid JSON: {exc}")

    web_base = web_base_override or config.get("webBase", "")
    return (web_base, config.get("spaceTitles", {}),
            config.get("spaceIds", {}), config.get("copyrightHolder", ""),
            config.get("coverSubtitle", "Complete documentation"))


CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome-stable", "google-chrome", "chromium-browser", "chromium",
]


def find_chrome():
    for cand in CHROME_CANDIDATES:
        if os.path.isabs(cand):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
        elif shutil.which(cand):
            return shutil.which(cand)
    sys.exit("error: no Chrome/Chromium binary found (see CHROME_CANDIDATES)")


def discover_spaces():
    """Return every directory holding a `SUMMARY.md`, in repository order.

    A GitBook space is exactly "a directory with a SUMMARY.md", so discovering
    them means a monorepo that gains a second space builds it without editing
    this file. `.gitbook/` is skipped: it holds includes and assets, not pages.
    """
    spaces = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d not in ("node_modules",
                                                              "dist", "pdf")]
        if "SUMMARY.md" in filenames:
            rel = os.path.relpath(dirpath, REPO_ROOT).replace(os.sep, "/")
            spaces.append("." if rel == "." else rel)
    return sorted(spaces)


def space_name(space):
    """The space's own directory name (`examples/product-docs` -> `product-docs`)."""
    return os.path.basename(REPO_ROOT if space == "." else space.rstrip("/"))


def space_title(space, space_titles):
    return space_titles.get(space, space_name(space).replace("-", " ").title())


def pdf_basename(space):
    """Default output name: the space path with `/` -> `-` (`docs` -> `docs`).

    The whole path rather than the last component, so `en/docs` and `fr/docs`
    built together in one `--all` run don't overwrite each other.
    """
    return space_name(space) if space == "." else space.strip("/").replace("/", "-")


# ---------------------------------------------------------------------------
# SUMMARY.md -> manifest
# ---------------------------------------------------------------------------

# `* [Title](path.md)`, with the optional link title GitBook writes for a
# renamed navigation entry: `[Toolbar on published sites](path.md "Toolbar")`.
# Without the `"..."` branch the quoted tooltip lands inside the path and the
# page is reported missing.
ENTRY_RE = re.compile(
    r"^(\s*)\*\s*\[(.+?)\]\(\s*<?(.+?)>?(?:\s+\"[^\"]*\")?\s*\)\s*$")

# `## Get Started <a href="#getting-started" id="getting-started"></a>` -- a
# part heading. GitBook stores the part's slug in that anchor, so it is reused as
# the PDF destination and the on-site anchor stays recognizable.
PART_RE = re.compile(r"^##\s+(.+?)\s*$")
PART_ANCHOR_RE = re.compile(r'<a\b[^>]*\bid="([^"]+)"[^>]*>\s*</a>\s*$')


class Entry:
    """One line of the navigation: a part heading or a page."""

    __slots__ = ("kind", "depth", "title", "path", "anchor")

    def __init__(self, kind, depth, title, path, anchor):
        self.kind, self.depth = kind, depth
        self.title, self.path, self.anchor = title, path, anchor

    @property
    def is_page(self):
        return self.kind == "page"


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


def page_anchor(path):
    """Stable, unique, short HTML id for a page.

    A slug alone can exceed the 127-byte limit PDF puts on name tokens, which
    makes readers reject the destination, so a deep path is truncated and
    disambiguated with a hash of the full path.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower().removesuffix(".md")).strip("-")
    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:8]
    return f"pg-{slug[-60:].lstrip('-')}-{digest}"


def parse_summary(space):
    """Return the navigation as `[Entry]`, in reading order.

    Indent width is measured rather than assumed, so a space using four-space
    nesting parses the same as a two-space one. Duplicate targets are emitted
    once, at first appearance: emitting a page twice would produce two sections
    competing for one anchor.
    """
    space_dir = os.path.join(REPO_ROOT, space)
    summary = os.path.join(space_dir, "SUMMARY.md")

    raw, indents = [], []
    with open(summary, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            pm = PART_RE.match(line)
            if pm:
                title = pm.group(1)
                am = PART_ANCHOR_RE.search(title)
                anchor = "part-" + (am.group(1) if am else slugify(title))
                if am:
                    title = title[:am.start()].rstrip()
                raw.append(("part", 0, unescape_md(title), None, anchor))
                continue
            m = ENTRY_RE.match(line)
            if not m:
                continue
            indent, title, target = len(m.group(1)), m.group(2), m.group(3)
            # An external navigation entry (a link to another site) is not a
            # page in this repository, so it cannot be a section of the PDF.
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # The path, like the title, can carry Markdown backslash escapes
            # (GitBook writes literal parens in a filename as `\(`/`\)`), so
            # it needs the same unescaping before it is checked against disk.
            raw.append(("page", indent, unescape_md(title),
                        unescape_md(target.split("#")[0]), None))
            indents.append(indent)

    depth_of = {ind: i for i, ind in enumerate(sorted(set(indents)))}
    # Parts occupy level 0, so pages sit one level deeper when any part exists.
    offset = 1 if any(kind == "part" for kind, *_ in raw) else 0

    entries, seen = [], set()
    for kind, indent, title, target, anchor in raw:
        if kind == "part":
            entries.append(Entry("part", 0, title, None, anchor))
            continue
        if target in seen:
            continue
        if not os.path.isfile(os.path.join(space_dir, target)):
            print(f"  warn: SUMMARY target missing, skipped: {target}",
                  file=sys.stderr)
            continue
        seen.add(target)
        entries.append(Entry("page", depth_of[indent] + offset, title, target,
                             page_anchor(target)))
    return entries


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_markdown(space, entries, web_base, space_ids):
    """Concatenate every page in navigation order as one Markdown document."""
    space_dir = os.path.join(REPO_ROOT, space)
    pages = [e for e in entries if e.is_page]
    anchors = {e.path: e.anchor for e in pages}
    uuid_anchors = build_uuid_anchor_map(space_dir, anchors)
    basename_anchors = build_basename_anchor_map(anchors)
    # Reference labels come from these, so a `{% content-ref %}` names a page
    # exactly as the contents list and the PDF outline do.
    titles = {e.path: e.title for e in pages}

    chunks = []
    for entry in entries:
        if not entry.is_page:
            # A part heading is a real level-1 section, so the PDF outline has
            # the same shape as the site's navigation.
            chunks.append(f"# {entry.title} {{#{entry.anchor} .part}}\n")
            continue
        abs_path = os.path.join(space_dir, entry.path)
        try:
            with open(abs_path, encoding="utf-8") as fh:
                src = fh.read()
        except OSError as exc:
            print(f"  warn: unreadable {entry.path}: {exc}", file=sys.stderr)
            continue
        chunks.append(preprocess(
            src,
            page_path=abs_path,
            space_dir=space_dir,
            anchors=anchors,
            heading_offset=entry.depth,
            web_base=web_base,
            anchor_id=entry.anchor,
            title=entry.title,
            titles=titles,
            space=space,
            space_ids=space_ids,
            uuid_anchors=uuid_anchors,
            basename_anchors=basename_anchors,
        ))
    return "\n\n".join(chunks)


# Only markers naming a real GitBook block are counted. A bare `{%` also appears
# in legitimate content, and flagging that would train readers to ignore the
# warning.
LEAK_RE = re.compile(
    r"\{%\s*(?:end)?(?:hint|tabs?|stepper|step|columns?|code|content-ref"
    r"|embed|file|include|openapi(?:-schemas)?|updates?|update|if|else)\b")

# A `/broken/` link is GitBook's marker for a dead internal reference. The
# preprocessor unlinks them; one surviving in a URL position means a shape the
# rewriter did not recognize, and it would print as a link that goes nowhere.
BROKEN_LINK_RE = re.compile(r"[(\"']/broken/(?:pages|files)/")

# An editor URL in a link position resolves for GitBook to the published site,
# but for nobody else. These docs also *mention* app.gitbook.com in prose
# perfectly legitimately, so only URLs sitting where a destination goes count.
EDITOR_LINK_RE = re.compile(
    r"[(\"']https?://app\.gitbook\.com/(?:o/[^/]+/)?s/([A-Za-z0-9_-]+)")

# The strongest link assertions are made on the generated HTML rather than on
# Markdown, because by then every destination is in its final form and can be
# classified exactly. Both of these are unambiguous defects: a relative path has
# nothing to resolve against inside a PDF, and a `file://` destination points at
# the build machine's disk.
HREF_RE = re.compile(r'href="([^"]*)"')
ABSOLUTE_HREF = ("#", "http://", "https://", "mailto:", "tel:", "data:")


def classify_hrefs(body_html):
    """Return `(relative, file_scheme)` counts for links in the built HTML."""
    relative = file_scheme = 0
    for url in HREF_RE.findall(body_html):
        if url.startswith("file://"):
            file_scheme += 1
        elif not url.startswith(ABSOLUTE_HREF):
            relative += 1
    return relative, file_scheme


def find_in_prose(markdown, pattern):
    """Return every match outside fenced and inline code.

    Code is skipped for the same reason the preprocessor skips it: these pages
    document GitBook syntax, so `{% hint %}` inside a fenced example is content.
    Counting those would make every build warn and the warning worthless.
    """
    found = []
    for line, in_code in iter_lines(markdown):
        if in_code:
            continue
        found.extend(pattern.findall(INLINE_CODE_RE.sub("", line)))
    return found


def count_in_prose(markdown, pattern):
    return len(find_in_prose(markdown, pattern))


def unmapped_space_ids(markdown, space_ids):
    """Space IDs linked in editor form that `space_ids` does not cover.

    These are cross-space references stored as `app.gitbook.com/s/<id>/...`.
    GitBook rewrites them to public URLs when it renders the site; a PDF cannot,
    so the link is left intact -- it still resolves for a signed-in reader, and
    inventing a public URL for a space whose published location is unknown would
    send everyone somewhere wrong instead.

    Reported as a note naming the IDs, because the fix is one line in
    `pdf.config.json`'s `spaceIds` and a bare count would not say which.
    """
    seen = {}
    for space_id in find_in_prose(markdown, EDITOR_LINK_RE):
        if space_id not in space_ids:
            seen[space_id] = seen.get(space_id, 0) + 1
    return seen


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------

def build_toc_html(entries):
    """A clickable contents list mirroring SUMMARY.md, parts included."""
    rows = []
    for entry in entries:
        if entry.is_page:
            cls = f"toc-d{min(entry.depth, 5)}"
        else:
            cls = "toc-part"
        rows.append(f'<li class="{cls}"><a href="#{entry.anchor}">'
                    f'{html.escape(entry.title)}</a></li>')
    return ('<nav class="toc"><h1>Contents</h1><ul>' + "".join(rows)
            + "</ul></nav>")


# Math and raw-TeX extensions must stay OFF. GitBook has no TeX support, so a
# `$` in shell prose is a dollar sign; leaving them on renders ordinary prose
# containing `$` as mangled equations. GitBook's own `$$...$$` spans are handled
# by the preprocessor instead.
PANDOC_FROM = (
    "markdown"
    "+fenced_divs+pipe_tables+raw_html+autolink_bare_uris"
    "+backtick_code_blocks+footnotes+strikeout+task_lists+header_attributes"
    "-tex_math_dollars-tex_math_single_backslash-tex_math_double_backslash"
    "-raw_tex-latex_macros-citations"
)


def run_pandoc(markdown, title):
    """Convert the assembled Markdown to an HTML fragment.

    Highlighting is off so the print stylesheet owns code styling and the
    intermediate HTML stays small. `--no-highlight` rather than the newer
    `--syntax-highlighting=none`: the latter needs pandoc 3.7, and the older
    flag is accepted by every 3.x, so a local build on a distro pandoc works.
    """
    proc = subprocess.run(
        ["pandoc", "--from", PANDOC_FROM, "--to", "html5",
         "--no-highlight", "--wrap", "none",
         "--metadata", f"title={title}"],
        input=markdown, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"error: pandoc failed:\n{proc.stderr[:4000]}")
    if proc.stderr.strip():
        print(f"  pandoc notes: {proc.stderr.strip()[:500]}", file=sys.stderr)
    return proc.stdout


MERMAID_JS = os.path.join(PDF_DIR, "vendor", "mermaid.min.js")


def load_mermaid():
    """Return the Mermaid bundle, or None to fall back to plain code blocks.

    The bundle is fetched by `pdf/fetch-deps.sh` rather than committed. Without
    it, a Mermaid fence prints as its source rather than failing the build.
    """
    if os.path.isfile(MERMAID_JS):
        with open(MERMAID_JS, encoding="utf-8") as fh:
            return fh.read()
    return None


LOGO_SVG = os.path.join(PDF_DIR, "assets", "logo.svg")
RASTER_LOGO_TYPES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
LOGO_TYPES = (".svg",) + RASTER_LOGO_TYPES


def _logo_markup(path):
    # An SVG is inlined so it stays vector; a raster image can't be, so
    # Chrome loads it off disk like a cover image.
    if path.lower().endswith(".svg"):
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    return f'<img src="file://{quote(os.path.abspath(path))}" alt="">'


def load_logo(logo_path=None):
    """Return the cover-page logo markup, or None to render without one.

    `logo_path` (relative to `REPO_ROOT`, i.e. the caller's own checkout) is
    what makes a per-consumer logo possible when the action is referenced
    remotely (`uses: owner/repo@ref`): `PDF_DIR` is this action's own
    directory then, not the caller's. SVG, PNG, JPEG, WebP and GIF work.

    An explicit `logo_path` that is missing or of another type is an error,
    not a silent logo-less cover. Without one, `pdf/assets/logo.svg` next to
    this script is used if a copied-in installation provides it.
    """
    if logo_path:
        path = (logo_path if os.path.isabs(logo_path)
                else os.path.join(REPO_ROOT, logo_path))
        if not path.lower().endswith(LOGO_TYPES):
            sys.exit(f"error: logo must be one of {', '.join(LOGO_TYPES)}: "
                     f"{logo_path}")
        if not os.path.isfile(path):
            sys.exit(f"error: logo not found: {path}")
        return _logo_markup(path)
    return _logo_markup(LOGO_SVG) if os.path.isfile(LOGO_SVG) else None


def load_cover_image(image_path):
    """Return a `file://` URL for a cover background image, or None.

    Resolved against `REPO_ROOT` (the caller's checkout) the same way
    `logo_path` is; like a raster logo, Chrome loads it directly off disk.
    """
    if not image_path:
        return None
    resolved = (image_path if os.path.isabs(image_path)
               else os.path.join(REPO_ROOT, image_path))
    return ("file://" + quote(os.path.abspath(resolved))
           if os.path.isfile(resolved) else None)


COVERS = ("blue", "light")

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _hex_color(value):
    # A form field invites `0043CE` as readily as `#0043CE`.
    value = value.strip()
    if value and not value.startswith("#"):
        value = "#" + value
    if not HEX_COLOR_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a 6-digit hex color, e.g. '#918BF7'")
    return value


def _relative_luminance(hex_color):
    """WCAG relative luminance of a `#rrggbb` color, in [0, 1]."""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = channel(r), channel(g), channel(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg, bg):
    """WCAG contrast ratio between two `#rrggbb` colors, in [1, 21]."""
    l1, l2 = _relative_luminance(fg), _relative_luminance(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


# A caller-supplied cover color has no pre-verified text pairing the way the
# two built-in treatments do (see check_contrast.py), so one is picked here
# at build time: whichever of ink or white contrasts better against it, with
# a matching secondary color from the same family that check_contrast.py
# already validates elsewhere in this stylesheet -- slate alongside ink,
# a pale blue-white alongside white, mirroring the light/dark cover pairs.
_INK = "#16202B"
_SLATE = "#4A5567"
_WHITE = "#ffffff"
_PALE = "#D6E2FF"


def pick_cover_text(bg_hex):
    """Return `(primary, secondary, worst_ratio)` for a custom cover color.

    `primary` covers both the (large) title and the (small, 8.5pt) legal
    link, so `worst_ratio` -- the lower of the two text colors' contrast --
    is checked against the small-text 4.5:1 floor, not just the large-text
    3:1 one, even though the title alone would clear the lower bar.
    """
    ink_contrast = _contrast_ratio(_INK, bg_hex)
    white_contrast = _contrast_ratio(_WHITE, bg_hex)
    if ink_contrast >= white_contrast:
        primary, primary_contrast, secondary = _INK, ink_contrast, _SLATE
    else:
        primary, primary_contrast, secondary = _WHITE, white_contrast, _PALE
    secondary_contrast = _contrast_ratio(secondary, bg_hex)
    return primary, secondary, min(primary_contrast, secondary_contrast)


def _hex_to_rgba(hex_color, alpha):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r}, {g}, {b}, {alpha})"


def build_html(space, body_html, toc_html, page_count, version_label,
               web_base, space_titles, copyright_holder, cover="blue",
               logo_path=None, cover_color=None, cover_image=None,
               subtitle="Complete documentation", extra_css_path=None):
    with open(os.path.join(PDF_DIR, "style.css"), encoding="utf-8") as fh:
        css = fh.read()
    # Appended after the base stylesheet so it wins the cascade. Every color
    # in style.css is a `:root` custom property, so a brand palette is a few
    # `--name: #hex;` overrides here rather than a fork of style.css.
    if extra_css_path:
        path = extra_css_path if os.path.isabs(extra_css_path) \
            else os.path.join(REPO_ROOT, extra_css_path)
        if not os.path.isfile(path):
            sys.exit(f"error: stylesheet not found: {path}")
        with open(path, encoding="utf-8") as fh:
            css += "\n/* --css */\n" + fh.read()

    mermaid_js = load_mermaid()
    # `__renderComplete` is set either way: it is the signal a future CDP-driven
    # renderer would wait on, and keeping it unconditional means the page has
    # the same contract with and without diagrams.
    mermaid_block = (
        f"<script>{mermaid_js}</script>\n<script>"
        "(async () => {"
        "  try {"
        "    mermaid.initialize({ startOnLoad: false, theme: 'neutral' });"
        "    await mermaid.run({ querySelector: 'pre.mermaid' });"
        "  } catch (e) { console.error('mermaid', e); }"
        "  window.__renderComplete = true;"
        "})();</script>"
        if mermaid_js else "<script>window.__renderComplete = true;</script>")

    title = space_title(space, space_titles)
    label = html.escape(version_label) if version_label else ""
    # Both are site-specific and optional -- a consumer with no config file
    # still gets a correct, if plainer, cover rather than another site's
    # copyright notice or a broken link.
    copyright_clause = (f"&copy; {html.escape(copyright_holder)}. "
                       if copyright_holder else "")
    web_base_clause = (
        f'Full documentation can be found at '
        f'<a href="{html.escape(web_base)}">{html.escape(web_base)}</a>.'
        if web_base else "")
    legal_clause = copyright_clause + web_base_clause
    logo = load_logo(logo_path)
    logo_block = f'<div class="cover-logo">{logo}</div>' if logo else ""

    cover_class = f"cover-{cover}"
    custom_style = ""
    image_url = load_cover_image(cover_image)
    if image_url:
        # A photo's own contrast can't be verified the way a flat color can
        # (that would take real pixel analysis, not just a hex value), so
        # legibility comes from a scrim instead: a translucent tint stacked
        # under text chosen for that tint. A plain black scrim (the default,
        # no cover-color given) reliably favors white/pale regardless of the
        # photo, since black darkens everything under it; a colored tint does
        # not carry that guarantee -- a light brand color needs the same
        # ink-or-white pick a flat cover-color background gets, or a bright
        # tint under white text (the unconditional old default) would fail
        # exactly where the tint itself is light.
        if cover_color:
            primary, secondary, worst = pick_cover_text(cover_color)
            if worst < 4.5:
                print(f"[{space}] warn: cover-image's scrim tint "
                      f"{cover_color} only reaches {worst:.2f}:1 contrast "
                      f"against its text (WCAG AA wants 4.5:1) -- consider "
                      f"a different color", file=sys.stderr)
        else:
            primary, secondary = _WHITE, _PALE
        scrim = (_hex_to_rgba(cover_color, 0.55) if cover_color
                else "rgba(0, 0, 0, 0.55)")
        cover_class = "cover-custom"
        custom_style = f"""<style>
.cover-custom {{
  background: linear-gradient({scrim}, {scrim}), url('{image_url}');
  background-size: cover;
  background-position: center;
  color: {primary};
}}
.cover-custom .subtitle,
.cover-custom .meta,
.cover-custom .legal {{ color: {secondary}; }}
.cover-custom a {{ color: {primary}; }}
</style>
"""
    elif cover_color:
        primary, secondary, worst = pick_cover_text(cover_color)
        if worst < 4.5:
            print(f"[{space}] warn: cover-color {cover_color} only reaches "
                  f"{worst:.2f}:1 contrast against its text (WCAG AA wants "
                  f"4.5:1) -- consider a different color",
                  file=sys.stderr)
        cover_class = "cover-custom"
        custom_style = f"""<style>
.cover-custom {{ background: {cover_color}; color: {primary}; }}
.cover-custom .subtitle,
.cover-custom .meta,
.cover-custom .legal {{ color: {secondary}; }}
.cover-custom a {{ color: {primary}; }}
</style>
"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>{css}</style>
{custom_style}</head><body>
<section class="cover {cover_class}">
  {logo_block}
  <h1>{html.escape(title)}</h1>
  <p class="subtitle">{html.escape(subtitle)}</p>
  <p class="meta">{label}{"<br>" if label else ""}{page_count} pages</p>
  <p class="legal">{legal_clause}</p>
</section>
{toc_html}
<main>
{body_html}
</main>
{mermaid_block}
</body></html>
"""


# ---------------------------------------------------------------------------
# Chrome
# ---------------------------------------------------------------------------

class RenderError(RuntimeError):
    """Chrome failed to produce a PDF for one space."""


# Headless Chrome writes a complete, valid PDF and then does not exit --
# reproduced here on Chrome 151 with a two-heading test page, so it is the norm
# rather than a large-document edge case. The PDF on disk is therefore the
# completion signal, not the process exit, and Chrome is terminated once its
# output stops growing.
#
# `--virtual-time-budget` advances a virtual clock and bounds nothing in
# wall-clock terms; CHROME_TIMEOUT_S is the only real ceiling.
CHROME_TIMEOUT_S = 1800
STABLE_CHECKS = 3          # consecutive equal sizes before the file is settled
POLL_INTERVAL_S = 1.0


def html_to_pdf(html_path, pdf_path, chrome):
    if os.path.exists(pdf_path):
        os.remove(pdf_path)     # a stale file would look like instant success

    profile = tempfile.mkdtemp(prefix="gitbook-pdf-chrome-")
    cmd = [
        chrome,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--no-pdf-header-footer",
        # Chrome's own header/footer would stamp the `file://` path of a temp
        # file on every page; the stylesheet owns the margins instead.
        "--allow-file-access-from-files",
        # Real PDF bookmarks from the heading structure. This is what makes a
        # 300-page document navigable in a reader, and it costs one flag.
        "--generate-pdf-document-outline",
        # A private profile never contends with another instance's singleton
        # lock, which otherwise blocks the run when a browser is already open.
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--disable-extensions",
        "--virtual-time-budget=600000",
        f"--print-to-pdf={pdf_path}",
        # Percent-encoded: an --out-dir containing a space would otherwise
        # truncate the URL and Chrome would print an error page instead.
        "file://" + quote(os.path.abspath(html_path)),
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + CHROME_TIMEOUT_S
    last_size, stable = -1, 0
    try:
        while True:
            if proc.poll() is not None:
                break           # exited on its own
            size = os.path.getsize(pdf_path) if os.path.isfile(pdf_path) else -1
            if size >= 0 and size == last_size:
                stable += 1
                if stable >= STABLE_CHECKS:
                    break       # output settled; Chrome is just not leaving
            else:
                stable = 0
            last_size = size
            if time.monotonic() > deadline:
                raise RenderError(
                    f"Chrome exceeded {CHROME_TIMEOUT_S}s with no complete PDF")
            time.sleep(POLL_INTERVAL_S)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=30)
        shutil.rmtree(profile, ignore_errors=True)

    if not os.path.isfile(pdf_path) or os.path.getsize(pdf_path) == 0:
        stderr = (proc.stderr.read() if proc.stderr else "")[:2000]
        raise RenderError(f"Chrome produced no PDF:\n{stderr}")


def optimize_pdf(path):
    """Losslessly shrink the PDF with qpdf, preserving every link.

    Chrome emits no object streams and leaves many streams uncompressed, so
    repacking is a substantial saving with links, named destinations, the
    document outline and page count all untouched.

    Do not substitute Ghostscript: it compresses harder but strips `/Link` and
    `/Dest`, which destroys every cross-reference and bookmark in the file.
    """
    if not shutil.which("qpdf"):
        print("  note: qpdf absent -- skipping lossless size optimization",
              file=sys.stderr)
        return

    tmp = path + ".opt"
    proc = subprocess.run(
        ["qpdf", "--object-streams=generate", "--compress-streams=y",
         "--recompress-flate", "--compression-level=9", "--linearize",
         path, tmp],
        capture_output=True, text=True)
    # qpdf exits 3 on recoverable warnings and still writes valid output.
    if proc.returncode not in (0, 3) or not os.path.isfile(tmp):
        print(f"  warn: qpdf failed, keeping unoptimized PDF: "
              f"{proc.stderr.strip()[:300]}", file=sys.stderr)
        if os.path.isfile(tmp):
            os.remove(tmp)
        return

    before, after = os.path.getsize(path), os.path.getsize(tmp)
    if after >= before:
        os.remove(tmp)
        return
    os.replace(tmp, path)
    print(f"  optimized: {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB "
          f"({100 * (1 - after / before):.0f}% smaller)")


def pdf_page_count(path):
    if not shutil.which("pdfinfo"):
        return "?"
    proc = subprocess.run(["pdfinfo", path], capture_output=True, text=True)
    m = re.search(r"^Pages:\s+(\d+)", proc.stdout, re.MULTILINE)
    return int(m.group(1)) if m else "?"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def build_space(space, out_dir, version_label, web_base, space_titles,
                space_ids, copyright_holder, limit=None, keep_html=False,
                no_optimize=False, cover="blue", logo_path=None,
                cover_color=None, cover_image=None, output_name=None,
                subtitle="Complete documentation", extra_css_path=None):
    print(f"[{space}] parsing SUMMARY.md")
    entries = parse_summary(space)
    if limit:
        # Truncate on pages, keeping the parts that precede them, so a limited
        # build still exercises the part headings.
        kept, pages = [], 0
        for entry in entries:
            if entry.is_page:
                if pages >= limit:
                    break
                pages += 1
            kept.append(entry)
        entries = kept
    pages = [e for e in entries if e.is_page]
    parts = len(entries) - len(pages)
    print(f"[{space}] {len(pages)} pages in {parts} parts")

    print(f"[{space}] preprocessing")
    unrecognized_markers.clear()
    markdown = build_markdown(space, entries, web_base, space_ids)
    unrecognized = dict(unrecognized_markers)
    # A note, not a strict check: the PDF is still usable, but a block the
    # preprocessor doesn't know lost its formatting, and this names which.
    if unrecognized:
        detail = ", ".join(f"{name} ({n})"
                           for name, n in sorted(unrecognized.items()))
        print(f"[{space}] note: GitBook markers with no specific handling "
              f"were dropped: {detail}", file=sys.stderr)

    checks = {
        "unconverted GitBook markers": count_in_prose(markdown, LEAK_RE),
        "dead /broken/ links": count_in_prose(markdown, BROKEN_LINK_RE),
    }

    unmapped = unmapped_space_ids(markdown, space_ids)
    if unmapped:
        detail = ", ".join(f"{sid} ({n})" for sid, n in sorted(unmapped.items()))
        print(f"[{space}] note: {sum(unmapped.values())} link(s) to unmapped "
              f"GitBook spaces, left as editor URLs: {detail}", file=sys.stderr)

    print(f"[{space}] pandoc")
    body_html = run_pandoc(markdown, space_title(space, space_titles))
    relative, file_scheme = classify_hrefs(body_html)
    checks["links with an unresolved relative path"] = relative
    checks["links pointing at file:// on the build machine"] = file_scheme

    for what, count in checks.items():
        if count:
            print(f"[{space}] warn: {count} {what}", file=sys.stderr)

    page = build_html(space, body_html, build_toc_html(entries), len(pages),
                      version_label, web_base, space_titles, copyright_holder,
                      cover, logo_path, cover_color, cover_image,
                      subtitle=subtitle, extra_css_path=extra_css_path)

    os.makedirs(out_dir, exist_ok=True)
    base = output_name.removesuffix(".pdf") if output_name else pdf_basename(space)
    html_path = os.path.join(out_dir, f"{base}.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(page)

    pdf_path = os.path.join(out_dir, f"{base}.pdf")
    print(f"[{space}] rendering PDF via Chrome")
    html_to_pdf(html_path, pdf_path, find_chrome())

    if not no_optimize:
        print(f"[{space}] optimizing")
        optimize_pdf(pdf_path)
    if not keep_html:
        os.remove(html_path)

    size_mb = os.path.getsize(pdf_path) / 1e6
    pdf_pages = pdf_page_count(pdf_path)
    result = {"space": space, "pdf": os.path.basename(pdf_path),
              "size_mb": round(size_mb, 2), "pdf_pages": pdf_pages,
              "doc_pages": len(pages), "parts": parts, "checks": checks,
              "unmapped_spaces": unmapped, "version_label": version_label,
              "web_base": web_base, "unrecognized_markers": unrecognized}

    # A sidecar manifest lets the workflow assert against what this build
    # actually contained -- "at least as many PDF pages as documentation pages"
    # holds for a two-page space and a thousand-page one alike, where a
    # hardcoded page floor would falsely fail small spaces.
    with open(os.path.join(out_dir, f"{base}.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)

    print(f"[{space}] done: {pdf_path} ({size_mb:.1f} MB, {pdf_pages} PDF pages)")
    return result


def main():
    global REPO_ROOT

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spaces", nargs="*", help="space directories to build")
    ap.add_argument("--all", action="store_true", help="build every space")
    ap.add_argument("--repo-root", default=None,
                    help="repository containing the docs to build "
                         "(default: current directory)")
    ap.add_argument("--out-dir", default=None,
                    help="default: <repo-root>/dist")
    ap.add_argument("--config", default=DEFAULT_CONFIG_FILE,
                    help=f"JSON file with webBase/spaceTitles/spaceIds/coverSubtitle/"
                         f"copyrightHolder, relative to repo-root unless "
                         f"absolute (default: {DEFAULT_CONFIG_FILE}; a "
                         f"missing file is not an error)")
    ap.add_argument("--web-base", default=None,
                    help="public URL the docs are published at, used to "
                         "rewrite outbound links; overrides webBase in "
                         "--config")
    ap.add_argument("--limit", type=int,
                    help="only the first N pages (testing)")
    ap.add_argument("--version-label", default=None,
                    help="cover-page text, e.g. 'Snapshot 2026-08-11'; "
                         "defaults to today's date. Pass '' to omit it.")
    ap.add_argument("--keep-html", action="store_true",
                    help="retain the intermediate HTML for debugging")
    ap.add_argument("--no-optimize", action="store_true",
                    help="skip the qpdf lossless size pass")
    ap.add_argument("--cover", choices=COVERS, default="blue",
                    help="cover treatment (both verified against WCAG AA); "
                         "ignored if --cover-color is set")
    ap.add_argument("--cover-color", default=None, type=_hex_color,
                    help="custom cover background, e.g. '#918BF7'. Text "
                         "color is picked automatically (whichever of ink "
                         "or white contrasts better) and a warning is "
                         "printed if that still falls short of WCAG AA. "
                         "Ignored (as a background) if --cover-image is "
                         "set, where it tints the scrim instead")
    ap.add_argument("--cover-image", default=None,
                    help="cover background photo, relative to repo-root "
                         "unless absolute. Rendered under a translucent "
                         "scrim (--cover-color's hue, or black by default) "
                         "with white text, since an arbitrary photo's own "
                         "contrast can't be verified the way a flat color "
                         "can. Overrides --cover/--cover-color as a background")
    ap.add_argument("--logo", default=None,
                    help="cover-page logo (SVG, PNG, JPEG, WebP or GIF), "
                         "relative to repo-root unless absolute; if unset, "
                         "pdf/assets/logo.svg next to this script is used "
                         "when present")
    ap.add_argument("--css", default=None,
                    help="extra stylesheet appended after pdf/style.css, "
                         "relative to repo-root unless absolute -- e.g. to "
                         "override the :root color variables with a brand "
                         "palette")
    ap.add_argument("--output-name", default=None,
                    help="PDF filename for a single-space build (default: "
                         "the space path with '/' -> '-', e.g. docs.pdf)")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any content check is non-zero "
                         "(used in CI, after the PDF has been written)")
    ap.add_argument("--list-spaces", action="store_true",
                    help="print the buildable spaces as JSON and exit; with "
                         "space arguments, validate them first (CI matrix)")
    args = ap.parse_args()

    if args.repo_root:
        REPO_ROOT = os.path.abspath(args.repo_root)
    out_dir = args.out_dir or os.path.join(REPO_ROOT, "dist")
    (web_base, space_titles, space_ids, copyright_holder,
     cover_subtitle) = load_config(REPO_ROOT, args.config, args.web_base)

    # A full run takes minutes; unbuffered output lets CI logs show which space
    # is in flight instead of nothing until the end.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    available = discover_spaces()
    if not available:
        sys.exit("error: no SUMMARY.md found -- is this a GitBook repository?")

    if args.list_spaces:
        # Validating here means a typo fails in seconds, before the CI matrix
        # has fanned out and spent a runner per bad name.
        wanted = args.spaces or available
        unknown = [s for s in wanted if s not in available]
        if unknown:
            ap.error(f"not a GitBook space: {', '.join(unknown)} "
                     f"(available: {', '.join(available)})")
        print(json.dumps(wanted))
        return 0

    spaces = available if args.all else args.spaces
    if not spaces:
        ap.error(f"name at least one space, or pass --all "
                 f"(available: {', '.join(available)})")
    unknown = [s for s in spaces if s not in available]
    if unknown:
        ap.error(f"not a GitBook space: {', '.join(unknown)}")
    if args.output_name and len(spaces) > 1:
        ap.error("--output-name needs exactly one space (several would "
                 "overwrite each other)")

    if not shutil.which("pandoc"):
        sys.exit("error: pandoc is not installed")

    # An undated snapshot of continuously-updated documentation cannot be placed
    # in time, and with an empty default, forgetting the flag drops the date
    # silently. The workflow resolves it the same way, so a local build and a CI
    # build label the cover identically.
    label = args.version_label
    if label is None:
        label = "Snapshot " + datetime.date.today().isoformat()
    print(f"cover label: {label or '(none)'}")

    # One space failing must not discard the others; the CI matrix disables
    # fail-fast for the same reason.
    results, failed = [], []
    for space in spaces:
        try:
            results.append(build_space(
                space, out_dir, label, web_base, space_titles, space_ids,
                copyright_holder, args.limit, args.keep_html,
                args.no_optimize, args.cover, args.logo, args.cover_color,
                args.cover_image, output_name=args.output_name,
                subtitle=cover_subtitle, extra_css_path=args.css))
        except RenderError as exc:
            print(f"[{space}] FAILED: {exc}", file=sys.stderr)
            failed.append(space)

    # Hands the real output path to the action's verify step, so the filename
    # rule (pdf_basename, --output-name) isn't duplicated in shell there.
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output and len(results) == 1:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"pdf-path={os.path.join(out_dir, results[0]['pdf'])}\n")

    print("\n=== summary ===")
    for r in results:
        flags = "  ".join(f"{n} {what}" for what, n in r["checks"].items() if n)
        print(f"{r['space']:16} {r['doc_pages']:4} docs  "
              f"{str(r['pdf_pages']):>6} pages  {r['size_mb']:6.1f} MB"
              + (f"  [{flags}]" if flags else ""))

    if failed:
        print(f"\nFAILED: {', '.join(failed)}", file=sys.stderr)
        return 1

    # `--strict` runs after every PDF is written, so a defect is reported with
    # the artifact still on disk to inspect rather than losing the whole build.
    if args.strict:
        offenders = [(r["space"], what, n) for r in results
                     for what, n in r["checks"].items() if n]
        if offenders:
            print("\nSTRICT: content checks failed", file=sys.stderr)
            for space, what, n in offenders:
                print(f"  {space}: {n} {what}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
