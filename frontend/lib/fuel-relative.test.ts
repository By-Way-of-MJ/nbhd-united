// Spec for the Fuel "Next up" wording. Pure functions only — runnable with
// Node's built-in runner after a tsc transpile (mirrors journal-date.test.ts).
import { test } from "node:test";
import assert from "node:assert/strict";

import { dayLabel, formatNextUpLabel, isOverdueLabel } from "./fuel-relative";

const L = "en-US";
// Wednesday 30 Sep 2026, 10:00 local.
const NOW = new Date(2026, 8, 30, 10, 0).getTime();
const at = (y: number, m: number, d: number, h: number, mi = 0) =>
  new Date(y, m - 1, d, h, mi).toISOString();

test("date-only today is 'Today', never a fake time", () => {
  assert.equal(formatNextUpLabel(null, "2026-09-30", NOW, L), "Today");
});

test("date-only tomorrow and later this week", () => {
  assert.equal(formatNextUpLabel(null, "2026-10-01", NOW, L), "Tomorrow");
  assert.equal(formatNextUpLabel(null, "2026-10-03", NOW, L), "Saturday");
});

test("date-only past date is Overdue with weekday and day", () => {
  const label = formatNextUpLabel(null, "2026-09-28", NOW, L);
  assert.equal(label, "Overdue · Mon 28");
  assert.equal(isOverdueLabel(label), true);
  assert.ok(!/AM|PM/.test(label));
});

test("timed past by hours is 'Overdue · <weekday> <time>'", () => {
  const label = formatNextUpLabel(at(2026, 9, 28, 9, 30), "2026-09-28", NOW, L);
  assert.match(label, /^Overdue · Mon,? 9:30\s?AM$/);
  assert.equal(isOverdueLabel(label), true);
});

test("timed just past is plain Overdue", () => {
  assert.equal(formatNextUpLabel(at(2026, 9, 30, 9, 0), "2026-09-30", NOW, L), "Overdue");
});

test("timed future keeps time-based wording", () => {
  assert.equal(formatNextUpLabel(at(2026, 9, 30, 10, 30), "2026-09-30", NOW, L), "In 30 min");
  assert.match(formatNextUpLabel(at(2026, 9, 30, 18, 30), "2026-09-30", NOW, L), /^Today at 6:30\s?PM$/);
  assert.match(formatNextUpLabel(at(2026, 10, 1, 7, 0), "2026-10-01", NOW, L), /^Tomorrow at 7:00\s?AM$/);
  assert.equal(isOverdueLabel("Today at 6:30 PM"), false);
});

test("dayLabel names today, tomorrow, then weekday + day", () => {
  const now = new Date(NOW);
  assert.equal(dayLabel(new Date(2026, 8, 30), now, L), "Today");
  assert.equal(dayLabel(new Date(2026, 9, 1), now, L), "Tomorrow");
  assert.equal(dayLabel(new Date(2026, 9, 3), now, L), "Sat 3");
});
