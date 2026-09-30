# How the PDF is built

Reference for `pdf/`: the pipeline, what it does with each piece of GitBook
markup, and why. For using the action, see the top-level `README.md`.

## Running it locally

```bash
python3 pdf/build.py docs                          # -> dist/docs.pdf
python3 pdf/build.py --all                         # every space in the repo
python3 pdf/build.py docs --limit 20 --keep-html   # fast iteration
./pdf/fetch-deps.sh                                # optional: Mermaid bundle
```

Requirements: `python3`, `pandoc` 3.x, Chrome or Chromium. Optional but
recommended: `qpdf` (lossless size pass), `poppler-utils` (page counts), `npm`
(only for `fetch-deps.sh`).

`build.py` builds the repository it's run from (`--repo-root` to point it
elsewhere). As a GitHub Action, that's the checkout of the repo using the
action, not this one.

## Configuration

Nothing in `build.py` is specific to a site. Site-specific values come from an
optional JSON file, `pdf.config.json` at the repo root by default (override with
`--config`):

```json
{
  "webBase": "https://example.com/docs",
  "spaceTitles": { "docs": "Example Documentation" },
  "spaceIds": { "<gitbook-space-id>": "docs" },
  "copyrightHolder": "Example Corp",
  "coverSubtitle": "Administrator guide"
}
```

All keys are optional and a missing file is not an error. `--web-base` (the
action's `web-base` input) overrides `webBase` for a one-off build. See *Links*
and *Cross-space links* below for what `webBase` and `spaceIds` do.

## Output

For the bundled example, `examples/product-docs`:

| Doc pages | Parts | PDF pages | Size | Build (local) |
|----------:|------:|----------:|-----:|--------------:|
| 13 | 4 | 25 | 0.4 MB | ~5 s |

Output scales with the source: a real space with a few hundred pages and many
screenshots produces a few hundred PDF pages and tens of MB, with size dominated
by the images. Beside each PDF, `<name>.json` records page counts, check
results, unmapped space IDs and unrecognized markers; the action's verify step
reads it.

## How it works

```
SUMMARY.md ─▶ manifest ─▶ preprocess ─▶ pandoc (md→html) ─▶ Chrome --print-to-pdf ─▶ qpdf
```

1. **`SUMMARY.md` is the manifest.** It is GitBook's navigation tree, so it
   supplies reading order, nesting depth and page titles. Depth becomes the
   heading level, which becomes the PDF outline; concatenating files
   alphabetically would scramble the order.

   Its `##` part headings are real level-1 sections, each starting a new page,
   so the document's spine matches the site's navigation. Pages absent from
   `SUMMARY.md` are unpublished and excluded on purpose. A page listed twice is
   emitted once, at first appearance, since two copies would compete for one
   anchor.

2. **`gitbook_preprocess.py` rewrites the GitBook blocks.** Pandoc doesn't know
   them and passes them through as literal text, so without this step readers
   see `{% hint style="info" %}` in the prose.

3. **Pandoc converts to an HTML fragment.** TeX math extensions are off: GitBook
   has no TeX support, so a `$` in shell prose is a dollar sign, and leaving
   math on renders ordinary prose as mangled equations.

4. **Chrome prints it.** Not LaTeX: GitBook pages carry raw HTML tables,
   `<details>` blocks and card grids a TeX engine can't render. Chrome also lets
   `style.css` own the layout, and `--generate-pdf-document-outline` gives real
   PDF bookmarks for one flag.

5. **`qpdf` repacks it losslessly.** See *Size*.

## What the preprocessor handles

Every transform is fence-aware: docs about GitBook often *show* GitBook syntax,
and `{% hint %}` inside a fenced example is content, not markup.

| Markup | Treatment |
|--------|-----------|
| `{% hint %}` | callout styled and labelled per severity (Note / Tip / Warning / Important) |
| `{% stepper %}` / `{% step %}` | explicitly numbered steps |
| `{% code title=… %}` | title emitted as a caption; `overflow`/`lineNumbers` dropped |
| `{% tabs %}` / `{% tab %}` | sequential labelled subsections |
| `{% include %}` | local files inlined; unresolvable ones dropped and reported |
| `{% embed %}` | reduced to a link |
| `{% content-ref %}` | "See:" box labelled with the target's real title |
| `{% if %}`, `{% updates %}` | wrapper dropped, body kept |
| `{% columns %}` / `{% column %}` | linearized (a printed page is one column) |
| `{% openapi %}` / `{% openapi-schemas %}` | named, with a pointer to the live spec |
| `{% file %}` | filename kept, dead destination dropped |
| card tables (`data-view="cards"`) | a list of cards; see below |
| `<picture>` | collapsed to its light-mode `<img>` |
| `<details>` | forced open |
| `<i class="fa-…">` | inline SVG (Font Awesome Free); dropped if not in the free set |
| `<button data-action="ask">` | dropped (GitBook Assistant buttons) |
| `<span data-option>` / emoji spans | unwrapped |
| ` ```mermaid ` fences | rendered by Mermaid in the browser |

Anything else in `{% … %}` is removed by a catch-all, and each such marker is
**reported by name** (`note: GitBook markers with no specific handling were
dropped: …`, and `unrecognized_markers` in the manifest). It's a note rather
than a `--strict` failure, since the PDF is still usable, but it means a block
lost its formatting and wants a rule here. Two real cases were found this way:
a hint with an `icon=` attribute printing without its callout, and card tables
whose `data-view` wasn't the first attribute printing as raw grids.

Four things need more than a regex:

- **Blocks span code fences.** A `{% stepper %}` opens, fenced samples follow,
  then `{% endstepper %}` closes, so one `re.DOTALL` open/close regex fails on
  real pages. Block conversion is a single line-oriented pass carrying fence
  state.
- **Markers are not always alone on a line.** `{% content-ref url="./" %} .
  {% endcontent-ref %}` occurs verbatim, so replacements inject their own line
  breaks.
- **Attribute values contain `%`.** `{% column width="50%" %}` is common. A
  pattern ending `[^%]*%\}` misses it, so every attribute-bearing pattern
  closes on a non-greedy `.*?%\}`.
- **Unbalanced blocks are closed per page.** A page that opens `{% hint %}`
  without closing it would otherwise drag every later page into the callout.
  Footnote labels are namespaced per page for the same reason.

### Card tables

A card table is a `<table>` whose columns are mostly `data-hidden`: the visible
ones hold the title and description, the hidden ones carry cover images and a
`data-card-target` link. Printed as a table it's unreadable, so it becomes a
list of cards: hidden columns are dropped except the target, whose URL becomes
the title's link; an icon-only cell is folded into the title; heading tags are
removed so card titles don't become PDF bookmarks. Cover images are dropped on
purpose (decorative, large, no information).

### Links

In-space links become internal PDF anchors, so cross-references work offline.
Directory-style links resolve to that directory's `README.md`. Links leaving the
space are rewritten against `webBase`. Image paths become absolute `file://`
URLs, percent-decoded first because the source encodes them and the filesystem
doesn't.

**Link text comes from page titles.** GitBook stores a page mention, a
`{% content-ref %}` and a card's link cell with the target's *filename* as
text (`permissions.md`, or `.` for `url="./"`) and shows the real title on the
site. A link whose text is exactly its target's filename gets the title from
`SUMMARY.md` (so it matches the contents list), falling back to the page's H1.
Authored link text is left alone.

Some links are deliberately **unlinked** (text kept, destination dropped)
rather than pointed somewhere plausible:

- **`/broken/pages/…` and `/broken/files/…`**: GitBook's marker for a dead
  internal reference. A reader who clicks a fabricated destination assumes the
  PDF is broken, not the source.
- **Links to local assets**: GitBook links an inserted image to the asset
  itself, and `{% file %}` links the attachment. A relative path means nothing
  inside a PDF.
- **Leftovers from migrated content**: `urn:` references, `UUID-<hex>.html`
  and `file://` links from a previous docs system, and doubled-URL typos
  (`http:/%20https:/…`). The UUID and `file://` forms are resolved to the right
  page first where the space holds enough information to do so.

The action asserts that none of these reach the finished PDF, because the
failure is invisible until someone clicks.

### Cross-space links

Some references are stored as app.gitbook.com **editor** URLs
(`app.gitbook.com/s/<space-id>/<path>`), which GitBook rewrites to public URLs
when it renders the site. Map a space ID to a directory in `spaceIds` and those
links become internal anchors (same space) or site links (another space in the
repo). An unmapped ID is **left intact and reported**, not guessed at: the URL
still works for a signed-in reader, whereas an invented public URL would send
everyone somewhere wrong. The build prints the IDs and records them in the
manifest.

## Brand colors and contrast

Every color in `style.css` is a `:root` custom property, and every intended
text/background pair of the default palette is checked against WCAG 2.1 AA:

```bash
python3 pdf/check_contrast.py   # exits non-zero on any failure; runs in CI
```

| Color | Hex | On white | Used for |
|-------|-----|---------:|----------|
| Blue (`--blue`) | `#2159CC` | 6.24:1 AA | links, headings, tab labels |
| Deep blue (`--blue-deep`) | `#103A8C` | 10.47:1 AAA | part titles, step labels |
| Slate (`--slate`) | `#4A5567` | 7.54:1 AAA | standfirst, captions, deep contents |
| Ink (`--ink`) | `#16202B` | 16.46:1 AAA | body text |
| Green / Amber / Red | `#1B6E45` / `#8A5A00` / `#B3261E` | 5.57 / 5.39 / 5.75 on their tints | Tip / Warning / Important labels |
| Teal (`--teal`) | `#0E7C86` | 4.95:1 | decorative rules only |

To rebrand, override the variables in a stylesheet passed with `--css` (the
action's `stylesheet` input). The check only covers the defaults, so verify
your own colors.

Three deliberate decisions:

- **Never `opacity` for secondary text.** Dimmed text can look fine on screen
  and measure under the AA floor. Secondary text uses explicit verified colors.
- **Warning and Important are off-palette.** Forcing amber and red into blues
  would make a warning look like a note. Color is never the only signal anyway:
  every callout carries a bold label, so severity survives greyscale printing
  and color-vision deficiency.
- **The checker states intent; the cascade can still violate it.**
  `check_contrast.py` asserts the intended pairs, not computed CSS. A generic
  `h1 { color: … }` once overrode the cover's text color while the check still
  passed, which is why `.cover h1 { color: inherit }` exists. If you add a cover
  treatment, look at the rendered page too.

### Cover

```bash
python3 pdf/build.py docs                           # white, ink text (default)
python3 pdf/build.py docs --cover light             # pale blue, deep-blue text
python3 pdf/build.py docs --cover-color '#0043CE'   # any color; text picked for contrast
python3 pdf/build.py docs --cover-image assets/cover.jpg
python3 pdf/build.py docs --logo assets/logo.png     # SVG, PNG, JPG, WebP or GIF
```

The cover carries a snapshot label that **defaults to today's date**:
continuously updated documentation needs a date to be placed in time. Pass
`--version-label "Release 4.2"` to set it, or `--version-label ""` to omit it.
The build echoes `cover label: …` first, so a missing date is never silent.

## Size

Chrome emits no object streams and leaves many streams uncompressed.
`qpdf --object-streams=generate --recompress-flate` typically saves 10-40% with
links, named destinations, the outline and page count intact. It runs
automatically when `qpdf` is on `PATH`; skip it with `--no-optimize`. Beyond
that, the lever is the source images (screenshots are often far wider than the
column they print in), not the compressor.

**Do not substitute Ghostscript.** It compresses harder but strips every
`/Link` and `/Dest`, destroying cross-reference navigation and bookmarks.

## Chrome does not reliably exit

Headless Chrome writes a complete, valid PDF and then keeps running, even for a
two-heading test page. `build.py` therefore treats **the output file as the
completion signal**: it polls the PDF's size and terminates Chrome once the size
has been stable for a few checks.

- **`--virtual-time-budget` bounds nothing in wall-clock terms.** It advances a
  virtual clock and won't stop a hung render; `CHROME_TIMEOUT_S` is the real
  ceiling.
- **Always pass `--user-data-dir`.** Without it Chrome shares the default
  profile and can block on the singleton lock when a browser is already open,
  which on a developer's machine it always is.

With `--all`, a space that fails is reported and skipped rather than aborting
the run.

## What gets checked

`--strict` makes `build.py` exit non-zero if any content check is non-zero. It
runs *after* the PDF is written, so the artifact is still there to inspect.
The action then checks the finished PDF, because a bad link can't be spotted by
eye in a long document:

| Check | Why |
|-------|-----|
| PDF pages ≥ documentation pages | Scale-free: a fixed floor would falsely fail a two-page space |
| no `/broken/` destinations | A dead reference reached a real link |
| no `file://` destinations | A link points at the build machine's disk |
| no `app.gitbook.com` destinations (`strict` only) | An editor URL that needs a login; gated by `strict` because another space may genuinely have no mapping |
| no `.md`-suffixed links into `webBase` | A page rewritten to the wrong form; some sites soft-404 these |
| `/Outlines` present | Bookmarks come from one Chrome flag and are easy to lose silently |
| internal destinations > 0 | The contents list is actually clickable |
| WCAG AA contrast | Checks `style.css`'s default palette |

Preprocessor regressions are covered by `pdf/tests/test_preprocess.py`
(`python3 -m unittest discover -s pdf/tests`), one test per markup shape that
once printed wrong.

## Known limitations

- **Tabs and conditionals lose their interactivity.** Every tab panel prints in
  sequence, and both branches of an `{% if %}` print.
- **No page numbers or running headers.** Chrome ignores CSS `@page` margin
  boxes. Navigation comes from the clickable contents list and the outline.
  Adding them would mean driving Chrome over the DevTools Protocol
  (`headerTemplate`) or a paged-media formatter.
- **Math is shown as source.** GitBook typesets `$$…$$` with KaTeX; here it
  renders as inline code.
- **Reusable content stored only in GitBook** (`~/reusable/…` includes) can't
  be fetched from a checkout and is dropped, reported once per target.
- **Cover images on cards are dropped**, and **headings flatten past six
  levels**.
- **Font Awesome icons need network access at build time**, unlike the other
  dependencies. Each icon is fetched once and cached in
  `pdf/vendor/fontawesome/`. With no network, or an icon from GitBook's larger
  Pro set rather than the ~2000 free icons, it's dropped with a one-per-name
  warning. Icons are Font Awesome Free,
  [CC BY 4.0](https://fontawesome.com/license/free).

## Files

| File | Purpose |
|------|---------|
| `build.py` | space discovery, `SUMMARY.md` manifest, assembly, Pandoc/Chrome/qpdf pipeline, CLI |
| `gitbook_preprocess.py` | GitBook-block rewriting, card tables, HTML normalization, link and asset rewriting |
| `style.css` | print stylesheet: palette, cover, contents, callouts, steps, cards, tables, code |
| `check_contrast.py` | asserts the default palette meets WCAG 2.1 AA |
| `fetch-deps.sh` | fetches the Mermaid bundle into `pdf/vendor/` (gitignored) |
| `tests/` | preprocessor regression tests |
| `../action.yml` | the GitHub Action: install, build, verify one space |
