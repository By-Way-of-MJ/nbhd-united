import { test } from "node:test";
import assert from "node:assert/strict";
import { groupOpenTasks, localMonday, openTaskCounts, topThreeTasks, taskPage } from "./horizons-tasks";

const now = new Date(2026, 8, 29, 15, 30);
const task = (id: string, due_date: string | null, status = "open", parent_goal_id: string | null = null) => ({ id, due_date, status, parent_goal_id });
test("due soon includes overdue, today and the inclusive seventh calendar day, sorted", () => {
  const tasks = [task("7", "2026-10-06"), task("today", "2026-09-29"), task("overdue", "2026-08-01"), task("tomorrow", "2026-09-30")];
  const original = [...tasks];
  assert.deepEqual(groupOpenTasks(tasks, now).dueSoon.map((t) => t.id), ["overdue", "today", "tomorrow", "7"]);
  assert.deepEqual(tasks, original);
});
test("anytime includes undated and later tasks; done and archived are excluded", () => {
  const tasks = [task("undated", null), task("later", "2026-10-07"), task("done", "2026-09-20", "done"), task("archived", null, "archived")];
  assert.deepEqual(groupOpenTasks(tasks, now).anytime.map((t) => t.id), ["undated", "later"]);
  assert.equal(groupOpenTasks(tasks, now).dueSoon.length, 0);
});
test("due soon crosses year boundaries and is independent of current clock time", () => {
  const tasks = [task("boundary", "2027-01-03"), task("later", "2027-01-04")];
  assert.deepEqual(groupOpenTasks(tasks, new Date(2026, 11, 27, 23, 59)).dueSoon.map((t) => t.id), ["boundary"]);
});
test("empty groups and per-goal open counts", () => {
  assert.deepEqual(groupOpenTasks([], now), { dueSoon: [], anytime: [] });
  assert.deepEqual([...openTaskCounts([task("1", null, "open", "g1"), task("2", null, "open", "g1"), task("3", null, "done", "g1"), task("4", null), task("5", null, "open", "g2")])], [["g1", 2], ["g2", 1]]);
});
test("local Monday starts at midnight, including Sunday and Monday boundaries", () => {
  for (const date of [new Date(2026, 8, 28, 0, 0), now, new Date(2026, 9, 4, 23, 59)]) {
    assert.equal(localMonday(date), new Date(2026, 8, 28).toISOString());
  }
  assert.equal(localMonday(new Date(2026, 9, 5)), new Date(2026, 9, 5).toISOString());
});
test("local Monday crosses years without mutating its argument", () => {
  const date = new Date(2027, 0, 1, 10);
  const before = date.getTime();
  assert.equal(localMonday(date), new Date(2026, 11, 28).toISOString());
  assert.equal(date.getTime(), before);
});
test("local Monday preserves the offset at midnight across DST", () => {
  // Also run this suite with TZ=America/New_York and TZ=Asia/Tokyo.
  for (const date of [new Date(2026, 2, 8, 23), new Date(2026, 10, 1, 23)]) {
    const monday = new Date(localMonday(date));
    assert.equal(monday.getDay(), 1); assert.equal(monday.getHours(), 0);
    assert.equal(monday.getMinutes(), 0);
    const expected = new Date(date.getFullYear(), date.getMonth(), date.getDate() - 6);
    assert.equal(monday.getTime(), expected.getTime());
  }
});

test("top three prioritizes all dated open tasks before undated work", () => {
  const rows = [ranked("any", null, 1), ranked("future", "2027-01-01", 4), ranked("today", "2026-09-29", 3), ranked("overdue", "2026-09-01", 2), ranked("done", "2026-08-01", 1, "done")];
  const original = [...rows];
  assert.deepEqual(topThreeTasks(rows).map((row) => row.id), ["overdue", "today", "future"]);
  assert.deepEqual(rows, original);
});
test("top three fills missing due dates with oldest-created anytime tasks", () => {
  const rows = [ranked("new", null, 9), ranked("old", null, 1), ranked("dated", "2027-01-01", 10), ranked("middle", null, 4)];
  assert.deepEqual(topThreeTasks(rows).map((row) => row.id), ["dated", "old", "middle"]);
});
test("top three breaks tied due dates by creation instant then ID", () => {
  const rows = [ranked("z", "2026-09-30", 2), ranked("b", "2026-09-30", 1), ranked("a", "2026-09-30", 1), ranked("early-due", "2026-09-29", 9)];
  assert.deepEqual(topThreeTasks(rows).map((row) => row.id), ["early-due", "a", "b"]);
  assert.deepEqual(topThreeTasks([...rows].reverse()), topThreeTasks(rows));
});
test("top three anytime ties are deterministic; empty, fewer than three, closed tasks", () => {
  assert.deepEqual(topThreeTasks([]), []);
  const rows = [ranked("b", null, 1), ranked("a", null, 1), ranked("archived", null, 0, "archived")];
  assert.deepEqual(topThreeTasks(rows).map((row) => row.id), ["a", "b"]);
  assert.deepEqual(topThreeTasks([ranked("done", null, 0, "done")]), []);
});
test("pagination starts with ten and exposes exact next-page counts through exhaustion", () => {
  const rows = Array.from({ length: 27 }, (_, i) => i);
  assert.deepEqual(taskPage(rows), { items: rows.slice(0, 10), total: 27, more: 10 });
  assert.deepEqual(taskPage(rows, 20), { items: rows.slice(0, 20), total: 27, more: 7 });
  assert.deepEqual(taskPage(rows, 30), { items: rows, total: 27, more: 0 });
  assert.deepEqual(taskPage([], 10), { items: [], total: 0, more: 0 });
  assert.equal(taskPage(rows.slice(0, 12)).more, 2);
  assert.equal(taskPage(rows.slice(0, 10)).more, 0);
});
test("goal counts include dated and anytime tasks and change after completion or reassignment", () => {
  const rows = [task("1", "2026-09-29", "open", "g1"), task("2", null, "open", "g1"), task("3", null, "archived", "g1"), task("4", null, "open", "g2"), task("5", null)];
  assert.equal(openTaskCounts(rows).get("g1"), 2);
  assert.equal(openTaskCounts(rows.map((row) => row.id === "1" ? { ...row, status: "done" } : row)).get("g1"), 1);
  const reassigned = openTaskCounts(rows.map((row) => row.id === "2" ? { ...row, parent_goal_id: "g2" } : row));
  assert.equal(reassigned.get("g1"), 1);
  assert.equal(reassigned.get("g2"), 2);
  assert.equal(reassigned.get("unknown") ?? 0, 0);
});

function ranked(id: string, due_date: string | null, day: number, status = "open") {
  return { ...task(id, due_date, status), created_at: new Date(Date.UTC(2026, 7, day + 1)).toISOString() };
}
