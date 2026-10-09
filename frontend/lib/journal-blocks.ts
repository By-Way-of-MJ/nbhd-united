/** Shared journal grammar: heading boundaries outside backtick/tilde fences.
 * Sources (including the always-present preamble) are lossless substrings.
 */
export interface JournalBlock {
  index: number;
  source: string;
  kind: "preamble" | "entry" | "assistant" | "heading" | "section";
  heading: string;
  title: string;
  body: string;
  time?: string;
  author?: string;
}

const ASSISTANT_TITLES = new Set(["Morning Report", "Weather", "News & Interests", "Today's Focus"]);

export function splitJournalBlocks(markdown: string): string[] {
  const starts = [0];
  let offset = 0;
  let fence: { marker: string; length: number } | undefined;
  for (const line of markdown.match(/[^\n]*\n|[^\n]+$/g) ?? []) {
    const text = line.replace(/\r?\n$/, "");
    const marker = text.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (fence) {
      if (marker && marker[1][0] === fence.marker && marker[1].length >= fence.length && !marker[2].trim()) fence = undefined;
    } else if (marker && !(marker[1][0] === "`" && marker[2].includes("`"))) {
      fence = { marker: marker[1][0], length: marker[1].length };
    } else if (/^#{1,6} /.test(text)) {
      starts.push(offset);
    }
    offset += line.length;
  }
  return starts.map((start, i) => markdown.slice(start, starts[i + 1] ?? markdown.length));
}

export function parseJournalBlocks(markdown: string): JournalBlock[] {
  return splitJournalBlocks(markdown).map((source, index) => {
    const match = source.match(/^(#{1,6}) ([^\r\n]*)(\r?\n|$)/);
    const base: JournalBlock = { index, source, kind: "preamble", heading: "", title: "", body: source };
    if (index === 0 || !match) return base;
    const heading = match[0];
    const title = match[2].trim();
    const body = source.slice(heading.length);
    const entry = match[1] === "###" ? title.match(/^(\d{2}:\d{2})(?: — (.+))?$/) : null;
    const kind = entry ? "entry" : match[1] === "##" && ASSISTANT_TITLES.has(title) ? "assistant" : !body.trim() ? "heading" : "section";
    return { ...base, heading, title, body, kind, ...(entry ? { time: entry[1], author: entry[2] } : {}) };
  });
}

export function blockDraft(block: JournalBlock): string {
  return block.kind === "heading" ? block.title : block.body;
}

export function rebuildJournalBlock(block: JournalBlock, draft: string): string {
  if (draft === blockDraft(block)) return block.source;
  const newline = block.heading.endsWith("\r\n") ? "\r\n" : "\n";
  if (block.kind === "heading") return `${block.heading.match(/^#+/)?.[0] ?? "#"} ${draft.replace(/[\r\n]+/g, " ")}${newline}${block.body}`;
  return `${block.heading.replace(/\r?\n$/, "")}${newline}${draft}`;
}
