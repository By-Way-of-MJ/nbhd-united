"""Portable, exact-substring fixtures for the web/iOS block parser contract."""

from django.test import SimpleTestCase

from .blocks import split_markdown_blocks


class SplitMarkdownBlocksTests(SimpleTestCase):
    def test_contract_fixtures(self):
        fixtures = [
            ("", [""]),
            ("plain\ntrailing text", ["plain\ntrailing text"]),
            ("# Title\nbody\n", ["", "# Title\nbody\n"]),
            ("preamble\n\n# Title\n## Section\ntrailing", ["preamble\n\n", "# Title\n", "## Section\ntrailing"]),
            (
                "# A\n## B\n### C\n#### D\n##### E\n###### F",
                ["", "# A\n", "## B\n", "### C\n", "#### D\n", "##### E\n", "###### F"],
            ),
            (
                " # indented\n#no-space\n####### seven\n#\ttab\n# yes",
                [" # indented\n#no-space\n####### seven\n#\ttab\n", "# yes"],
            ),
            ("# A\n```python\n# code\n## code\n```\n# B", ["", "# A\n```python\n# code\n## code\n```\n", "# B"]),
            ("~~~\n# code\n~~~\n# B", ["~~~\n# code\n~~~\n", "# B"]),
            (
                "# A\n````\n```\n# code\n~~~~\n# still code\n`````\n# B",
                ["", "# A\n````\n```\n# code\n~~~~\n# still code\n`````\n", "# B"],
            ),
            ("# A\n```\n# unclosed", ["", "# A\n```\n# unclosed"]),
            ("  ~~~lang\n# code\n   ~~~~ \t\n# B", ["  ~~~lang\n# code\n   ~~~~ \t\n", "# B"]),
            ("    ```\n# heading", ["    ```\n", "# heading"]),
            ("```bad`info\n# heading", ["```bad`info\n", "# heading"]),
            ("```\n```not a close\n# code\n```\n# B", ["```\n```not a close\n# code\n```\n", "# B"]),
            (
                "前文\r\n# 日記\r\n```\r\n# code\r\n```\r\n## end\r\n",
                ["前文\r\n", "# 日記\r\n```\r\n# code\r\n```\r\n", "## end\r\n"],
            ),
            ("prose\u2028# not a line\n# heading", ["prose\u2028# not a line\n", "# heading"]),
        ]
        for markdown, expected in fixtures:
            with self.subTest(markdown=markdown):
                blocks = split_markdown_blocks(markdown)
                self.assertEqual(blocks, expected)
                self.assertEqual("".join(blocks).encode(), markdown.encode())
