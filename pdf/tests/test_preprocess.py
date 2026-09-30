"""Regression tests for GitBook markup shapes the preprocessor has missed.

    python3 -m unittest discover -s pdf/tests

Each case is markup GitBook really emits (most from the gitbook-templates
spaces in examples/) that once printed wrong without any check noticing.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build  # noqa: E402
import gitbook_preprocess as gp  # noqa: E402

SPACE = "/space"
PAGE = os.path.join(SPACE, "README.md")
ANCHORS = {"guides/automations.md": "pg-automations",
           "getting-started/quickstart.md": "pg-quickstart"}
TITLES = {"guides/automations.md": "Automations",
          "getting-started/quickstart.md": "Quickstart"}


def rewrite(text):
    return gp.rewrite_links(
        text, PAGE, SPACE, ANCHORS, "", {}, "space",
        ref_label=gp.make_ref_label(PAGE, SPACE, TITLES))


class CardTables(unittest.TestCase):
    def test_data_view_need_not_be_first_attribute(self):
        html = ('<table data-card-size="large" data-view="cards"><thead><tr>'
                '<th></th></tr></thead><tbody><tr><td>One</td></tr></tbody>'
                '</table>')
        out = gp.convert_cards(html)
        self.assertIn('<ul class="cards">', out)
        self.assertNotIn("<table", out)

    def test_icon_cell_joins_title_and_headings_are_dropped(self):
        html = ('<table data-view="cards"><thead><tr><th></th><th></th>'
                '<th></th></tr></thead><tbody><tr>'
                '<td><h4><i class="fa-book" style="color:$primary;">:book:</i>'
                '</h4></td><td><h4>Core concepts</h4></td><td>Details</td>'
                '</tr></tbody></table>')
        out = gp.convert_cards(html)
        self.assertRegex(out, r'<p class="card-title"><i class="fa-book"[^>]*>'
                              r':book:</i> Core concepts</p>')
        self.assertIn("<div>Details</div>", out)
        self.assertNotIn("<h4", out)


class Hints(unittest.TestCase):
    def test_hint_with_extra_attributes_keeps_its_callout(self):
        out = gp.convert_blocks('{% hint style="success" icon="sparkles" %}\n'
                                'Body\n{% endhint %}')
        self.assertIn(".hint-success", out)
        self.assertIn("**Tip:**", out)
        self.assertNotIn("{%", out)


class UnrecognizedMarkers(unittest.TestCase):
    def setUp(self):
        gp.unrecognized_markers.clear()

    def test_catch_all_records_what_it_drops(self):
        gp.convert_blocks("before {% mystery-block foo=1 %} after")
        self.assertEqual(gp.unrecognized_markers, {"mystery-block": 1})

    def test_handled_blocks_are_not_recorded(self):
        gp.convert_blocks('{% hint style="info" %}\nx\n{% endhint %}\n'
                          '{% if visitor.claims.x %}y{% endif %}')
        self.assertEqual(gp.unrecognized_markers, {})


class AssistantButtons(unittest.TestCase):
    def test_ask_buttons_dropped_link_buttons_kept(self):
        out = gp.normalize_html(
            '<button type="button" class="button primary" data-action="ask" '
            'data-icon="gitbook-assistant">Ask a question…</button>'
            '<a href="https://example.com" class="button secondary">Go</a>')
        self.assertNotIn("<button", out)
        self.assertIn('class="button secondary">Go</a>', out)


class FilenameLabels(unittest.TestCase):
    def test_mention_link_gets_page_title(self):
        out = rewrite('See [automations.md](guides/automations.md "mention").')
        self.assertEqual(out, "See [Automations](#pg-automations).")

    def test_html_card_cell_gets_page_title(self):
        out = rewrite('<a href="getting-started/quickstart.md">quickstart.md</a>')
        self.assertEqual(out, '<a href="#pg-quickstart">Quickstart</a>')

    def test_authored_label_is_left_alone(self):
        out = rewrite("[the automations guide](guides/automations.md)")
        self.assertEqual(out, "[the automations guide](#pg-automations)")


class OutputNames(unittest.TestCase):
    def test_default_name_is_the_space_path(self):
        self.assertEqual(build.pdf_basename("docs"), "docs")
        self.assertEqual(build.pdf_basename("en/docs"), "en-docs")

    def test_cover_color_accepts_a_missing_hash(self):
        self.assertEqual(build._hex_color("0043CE"), "#0043CE")
        self.assertEqual(build._hex_color("#0043ce"), "#0043ce")

    def test_title_falls_back_to_the_last_directory(self):
        self.assertEqual(build.space_title("examples/product-docs", {}),
                         "Product Docs")


class Logos(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()

    def write(self, name, data=b""):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_svg_is_inlined(self):
        path = self.write("logo.svg", b'<svg viewBox="0 0 1 1"></svg>')
        self.assertEqual(build.load_logo(path), '<svg viewBox="0 0 1 1"></svg>')

    def test_png_and_jpg_become_images(self):
        for name in ("logo.png", "logo.JPG"):
            markup = build.load_logo(self.write(name, b"\x89PNG"))
            self.assertTrue(markup.startswith('<img src="file://'), markup)
            self.assertIn(name, markup)

    def test_missing_or_unsupported_logo_fails_the_build(self):
        with self.assertRaises(SystemExit):
            build.load_logo(os.path.join(self.dir, "nope.png"))
        with self.assertRaises(SystemExit):
            build.load_logo(self.write("logo.pdf"))


if __name__ == "__main__":
    unittest.main()
