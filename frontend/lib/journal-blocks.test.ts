import { test } from "node:test";
import assert from "node:assert/strict";
import { blockDraft, parseJournalBlocks, rebuildJournalBlock, splitJournalBlocks } from "./journal-blocks";

const documents = [
  "", "Only a preamble", "# Heading", "# Heading\n", "intro\n\n# One\nbody\n## Two\ntrailing text",
  "### 07:40 — MJ\nCoffee first.\n\n### 08:20\nA second entry.\n",
  "## Morning Report\nA calm day.\n\n## Weather\nClear\n## News & Interests\nNews\n## Today's Focus\nWrite\n",
  "## List\n- [ ] Open\n- [x] Done\n", "preamble\r\n# Title\r\nbody\r\n## Next\r\n",
  "# Code\n```md\n# not a heading\n## nor this\n```\n## Real\nEnd",
  "~~~\n# fenced preamble\n~~~\n# Heading\n",
  "# Code\n````\n```\n# still inside\n````\n# Real\n",
];
for (const [i, input] of documents.entries()) {
  test(`lossless split and no-op rebuild round trip ${i}`, () => {
    assert.equal(splitJournalBlocks(input).join(""), input);
    const blocks = parseJournalBlocks(input);
    assert.equal(blocks.map((b) => b.source).join(""), input);
    for (const block of blocks.filter((b) => b.kind !== "preamble")) assert.equal(rebuildJournalBlock(block, blockDraft(block)), block.source);
  });
}
test("preamble always occupies index zero, even when empty", () => {
  assert.deepEqual(splitJournalBlocks("# Heading\nbody"), ["", "# Heading\nbody"]);
  assert.equal(parseJournalBlocks("intro\n# Heading")[0].kind, "preamble");
  assert.deepEqual(splitJournalBlocks(""), [""]);
});
test("all six heading levels split; seven hashes, tabs, and indented headings do not", () => {
  assert.equal(splitJournalBlocks("# 1\n## 2\n### 3\n#### 4\n##### 5\n###### 6\n####### 7\n#\ttab\n # indented").length, 7);
});
test("backtick and tilde fences suppress heading boundaries", () => {
  for (const fence of ["```", "~~~"]) {
    const blocks = splitJournalBlocks(`# Before\n${fence}md\n# code\n## more\n${fence}\n## After\n`);
    assert.equal(blocks.length, 3);
    assert.ok(blocks[1].includes("# code\n## more"));
  }
});
test("closing fence must match marker, minimum length and have no trailing text", () => {
  const input = "# First\n````\n~~~\n```\n```` invalid\n# inside\n````\n# Outside";
  assert.equal(splitJournalBlocks(input).length, 3);
  assert.equal(splitJournalBlocks("# First\n```\n# unclosed").length, 2);
});
test("indented fences and CRLF fences are recognized", () => {
  assert.equal(splitJournalBlocks("# First\r\n   ~~~js\r\n# code\r\n   ~~~\r\n## Last").length, 3);
});
test("entry with an author keeps time, author and exact body", () => {
  const b = parseJournalBlocks("### 07:40 — MJ\nCoffee.\n\n")[1];
  assert.equal(b.kind, "entry"); assert.equal(b.time, "07:40"); assert.equal(b.author, "MJ");
  assert.equal(b.body, "Coffee.\n\n");
  assert.equal(rebuildJournalBlock(b, "Tea."), "### 07:40 — MJ\nTea.");
});
test("entry without a name and empty entry remain entries", () => {
  const b = parseJournalBlocks("### 07:40\n")[1];
  assert.equal(b.kind, "entry"); assert.equal(b.author, undefined);
  assert.equal(parseJournalBlocks("## 07:40\nbody")[1].kind, "section");
});
test("only exact assistant titles at level two classify as assistant", () => {
  for (const title of ["Morning Report", "Weather", "News & Interests", "Today's Focus"]) {
    assert.equal(parseJournalBlocks(`## ${title}\nbody`)[1].kind, "assistant");
    assert.equal(parseJournalBlocks(`### ${title}\nbody`)[1].kind, "section");
  }
  assert.equal(parseJournalBlocks("## morning report\nbody")[1].kind, "section");
});
test("heading-only draft edits title and retains heading level and blank lines", () => {
  const b = parseJournalBlocks("#### A thought\n\n")[1];
  assert.equal(b.kind, "heading"); assert.equal(blockDraft(b), "A thought");
  assert.equal(rebuildJournalBlock(b, "Another thought"), "#### Another thought\n\n");
});
test("checkboxes remain exact body text, never separate blocks", () => {
  const body = "- [ ] One\n- [x] Two\n- [X] Three\n";
  assert.equal(parseJournalBlocks(`## List\n${body}`)[1].body, body);
  assert.equal(splitJournalBlocks(`## List\n${body}`).length, 2);
});
test("CRLF header is retained when rebuilding a changed entry", () => {
  const b = parseJournalBlocks("### 07:40 — MJ\r\nHello\r\n")[1];
  assert.equal(b.title, "07:40 — MJ");
  assert.equal(rebuildJournalBlock(b, "Updated\r\n"), "### 07:40 — MJ\r\nUpdated\r\n");
});
test("heading at EOF gains separator when a body is added", () => {
  const b = parseJournalBlocks("### 07:40")[1];
  assert.equal(rebuildJournalBlock(b, "New body"), "### 07:40\nNew body");
});
