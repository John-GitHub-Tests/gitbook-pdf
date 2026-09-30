# GitBook → PDF export

A GitHub Action that renders a whole GitBook space into a single print-quality,
bookmarked, cross-linked PDF — built from the Markdown your GitBook Git Sync
already publishes into this repo.

## What it does

- Builds the GitBook space you point it at (any directory containing a
  `SUMMARY.md`), following its navigation for order, nesting and titles
- Concatenates every page in navigation order, converting GitBook's blocks
  (hints, tabs, cards, steppers, math, Mermaid diagrams) to plain Markdown/HTML
- Rewrites every link: in-space links become PDF anchors, images become
  embedded assets, links outside the space point at your live site, and
  genuinely dead references get their destination dropped (label kept) rather
  than left broken or guessed at
- Renders via pandoc + headless Chrome, with a styled, WCAG-AA-checked cover
  page (title, snapshot date, page count)
- Optimizes losslessly with `qpdf` (typically 10–40% smaller)
- Verifies the result: page-count sanity, no dead links, has a real outline,
  has clickable internal cross-references

## Quick start

In the repo your GitBook space syncs to, add `.github/workflows/pdf.yml`:

```yaml
name: Docs PDF
on:
  push:
    branches: [main]
  workflow_dispatch:
jobs:
  pdf:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - id: pdf
        uses: John-GitHub-Tests/gitbook-pdf@v1
        with:
          space: docs          # the directory that contains SUMMARY.md
      - uses: actions/upload-artifact@v7
        with:
          name: docs-pdf
          path: ${{ steps.pdf.outputs.pdf-path }}
```

Push, and the PDF appears under the run's **Artifacts**. Two ready-to-copy
workflows are in [`templates/`](templates/):

- [`pdf-on-push.yml`](templates/pdf-on-push.yml): rebuild whenever the docs
  change, keep the PDF as an artifact
- [`pdf-on-release.yml`](templates/pdf-on-release.yml): attach the PDF to every
  GitHub release you publish

## See what you get

`examples/product-docs` is GitBook's
[Product docs template](https://github.com/GitBookIO/gitbook-templates/tree/main/product-docs),
included only to show the action working: its content is placeholder docs for
a fictional product. To build it on demand, go to **Actions → Generate PDF →
Run workflow** (you can set the space, cover color, logo and cover label
there); the PDF is under the run's **Artifacts**. CI
also builds it on every push as `sample-pdf`. Or build it locally:

```bash
python3 pdf/build.py examples/product-docs --out-dir /tmp/out
```

## Configure it

Every setting below is optional; `space` is the only required input.

1. Add a `pdf.config.json` at your repo root — every key is optional:
   ```json
   {
     "webBase": "https://your-docs-site.example.com",
     "spaceTitles": { "docs": "Your Product Documentation" },
     "spaceIds": { "<gitbook-space-id>": "docs" },
     "copyrightHolder": "Your Company",
     "coverSubtitle": "Administrator guide"
   }
   ```
   - `webBase` is what outbound (out-of-space) links get rewritten against;
     without it, those links are left unresolved.
   - `spaceTitles` overrides a space's PDF title; without it, the title falls
     back to the directory name (e.g. `product-docs` → "Product Docs").
   - `spaceIds` only matters if some pages contain cross-space links stored as
     `app.gitbook.com` editor URLs — see `pdf/README.md` for when that applies.
   - `copyrightHolder` adds a `© Your Company` line to the cover, if you want
     one. Leave it out for a plainer cover with no copyright line.
   - `coverSubtitle` replaces the cover's "Complete documentation" line.

   Keep it somewhere else by passing `config-file: path/to/pdf.config.json`.
2. Name the file with `output-name: handbook.pdf` (default: the space path,
   so `docs` gives `docs.pdf`). The `pdf-path` output always holds the full
   path, as in the Quick start above.
3. Optional: point `logo-path` at a logo file in your own repo for the cover
   page (SVG, PNG, JPG, WebP or GIF):
   ```yaml
   - uses: John-GitHub-Tests/gitbook-pdf@v1
     with:
       space: docs
       logo-path: assets/logo.png   # a path in your repo
   ```
   Without `logo-path`, the cover renders with no logo. It sits on a white
   plate in the top corner, so a transparent PNG or an SVG works best.
4. What you do with the PDF afterwards — workflow artifact, GitHub Release,
   commit it somewhere — is up to your workflow; `templates/` covers the two
   common cases. For several GitBook spaces in one repo, call the action once
   per space: one step each, or a job matrix over the space directories.

If instead you want your own pinned, modifiable copy of the tool rather than
depending on this repo, copy `pdf/` and `action.yml` into your repo at the
same relative paths and reference `./` instead of `John-GitHub-Tests/gitbook-pdf@v1`.

Full input list is documented in `action.yml`; the build pipeline itself
(what it does with GitBook's custom blocks, links, images, contrast, Chrome
quirks) is documented in `pdf/README.md`.

## Cover treatments

Two are built in, chosen via the action's `cover` input:

- `blue` (default) — plain white background, dark text
- `light` — a very light blue-tinted background

Both are contrast-checked against WCAG AA by `pdf/check_contrast.py`; run it
after any `pdf/style.css` edit to confirm a change hasn't broken that.

For your own brand color, set `cover-color` to a 6-digit hex value instead
(overrides `cover`):

```yaml
- uses: John-GitHub-Tests/gitbook-pdf@v1
  with:
    space: docs
    cover-color: '#0043CE'
```

Text color (ink or white, whichever contrasts better) is picked
automatically, since an arbitrary color has no pre-verified pairing the way
`blue`/`light` do. If neither reaches WCAG AA's 4.5:1 against your color —
some colors, especially mid-tone ones, don't pair well with either — the
build still succeeds but prints a warning naming the actual ratio, so you
find out from the build log rather than by someone squinting at the PDF.

For a background photo, set `cover-image` (overrides `cover`/`cover-color`
as a background):

```yaml
- uses: John-GitHub-Tests/gitbook-pdf@v1
  with:
    space: docs
    cover-image: assets/cover-photo.jpg   # resolved against your checkout
    cover-color: '#0043CE'                # optional: tints the scrim
```

An arbitrary photo's own contrast can't be verified the way a flat color
can — that would take real pixel analysis, not just a hex value — so
legibility comes from a translucent scrim instead: black by default (which
reliably favors white text regardless of the photo), or `cover-color`'s hue
if you set one, in which case the *scrim* gets the same automatic ink/white
pick and same WCAG AA warning `cover-color` alone gets. A busy or
high-contrast photo can still make small text harder to read than the
warning alone would suggest — check the actual PDF for anything you're
shipping externally.

## Brand colors inside the document

`cover-color` only affects the cover. To recolor links, headings, callouts
and the rest, pass your own stylesheet; it's appended after the built-in one,
and every color there is a CSS variable:

```css
/* assets/pdf-brand.css */
:root {
  --blue: #0043CE;       /* links, headings, tab labels */
  --blue-deep: #002D9C;  /* part titles, step labels */
}
```

```yaml
- uses: John-GitHub-Tests/gitbook-pdf@v1
  with:
    space: docs
    stylesheet: assets/pdf-brand.css
```

The full list of variables is at the top of `pdf/style.css`. The built-in
contrast check only covers the default palette, so check your own colors
against white (at least 4.5:1 for body-size text).

## Performance

Every dependency this action fetches over the network -- pandoc, the Mermaid
bundle, Font Awesome icons -- is cached via `actions/cache`, keyed by version
(or, for icons, restored from the most recent prior run and always re-saved,
since that set grows over time). A cold run pays the full fetch cost once;
every run after that in the same repo skips it. Chrome's install is not
cached: measured against a real run, its `.deb` download is a small fraction
of that step's time (the bulk is `apt-get`'s own unpack/setup work, not
avoidable without bypassing `dpkg` -- not a trade worth making for what
would be a marginal gain on a component whose sandboxing this pipeline
already depends on behaving exactly as its official install leaves it).

## Requirements

- `ubuntu-latest` (or another x86-64 Linux runner) — the action installs
  Chrome, pandoc, and `qpdf`/`poppler-utils` via `apt`. Self-hosted runners
  need Actions Runner 2.327.1 or later.
- A GitBook space Git-synced into the repo (or any repo with a `SUMMARY.md`
  and matching page structure — GitBook Git Sync is the common case, not a
  hard requirement)

## Development

```bash
python3 -m unittest discover -s pdf/tests   # preprocessor regression tests
python3 pdf/check_contrast.py               # WCAG AA check of style.css
python3 pdf/build.py examples/product-docs --out-dir /tmp/out --strict
```

CI (`.github/workflows/ci.yml`) runs all three on every push and pull
request, the last through the action itself.

## License

The tool is MIT licensed (see `LICENSE`). `examples/product-docs` comes from
GitBook's gitbook-templates repository and is GPL-3.0 (see
`examples/LICENSE` and `examples/README.md`); it isn't needed to use the
action.
