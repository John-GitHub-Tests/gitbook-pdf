# Example space

`product-docs/` is GitBook's **Product docs** template from the
[gitbook-templates](https://github.com/GitBookIO/gitbook-templates)
repository. It is here only to show the action working without needing docs
of your own; the content is placeholder documentation for a fictional
product.

It's a normal GitBook space (a directory with a `SUMMARY.md`) and uses most
of GitBook's blocks: hints, tabs, steppers, cards, content-refs, columns,
expandables, tables and a Mermaid diagram. Build it locally:

```bash
python3 pdf/build.py examples/product-docs --out-dir /tmp/out
```

CI builds it on every push; download `sample-pdf` from the latest CI run to
see the result.

## License and changes

This directory is licensed under the GNU GPL v3 (see `LICENSE` here), which
applies only to the example space. The rest of the repository is MIT (see
`../LICENSE`).

Modified from the upstream template on 2026-09-30: the template contains 10
dead internal links (GitBook's `/broken/pages/<id>` form) in `README.md`,
`getting-started/getting-started.md`, `getting-started/your-first-project.md`,
`guides/guides.md` and `reference/reference.md`. Each was pointed at the page
it evidently meant, going by the card or link's own title. No other content
was changed.
