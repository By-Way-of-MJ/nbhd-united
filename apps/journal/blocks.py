"""Lossless heading-block contract shared with the web and iOS editors."""

import re


def split_markdown_blocks(markdown: str) -> list[str]:
    """Split at lines matching ``^#{1,6} `` outside fenced code blocks.

    Block 0 is always the preamble (including an empty string). Each subsequent
    block starts at its heading and ends immediately before the next heading,
    or at EOF. Lines are delimited by LF; CRLF and all other bytes are retained,
    so ``"".join(blocks) == markdown`` even without a final newline.

    A fence opens with 0–3 ASCII spaces followed by at least three backticks or
    tildes; trailing info is allowed (but a backtick opener's info cannot contain
    backticks). It closes only with 0–3 spaces, the same character repeated at
    least as many times, then only spaces/tabs. Other fences do not nest; an
    unclosed fence consumes through EOF. Fence markers are never headings.
    """
    starts = [0]
    offset = 0
    fence = None
    for line in markdown.split("\n"):
        # Only LF is a line boundary; splitlines also splits Unicode prose.
        source = line.removesuffix("\r")
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", source)
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip(" \t"):
                fence = None
        elif marker and (marker[1][0] != "`" or "`" not in marker[2]):
            fence = marker[1]
        elif re.match(r"^#{1,6} ", source):
            starts.append(offset)
        offset += len(line) + 1
    return [markdown[start:end] for start, end in zip(starts, [*starts[1:], len(markdown)])]
