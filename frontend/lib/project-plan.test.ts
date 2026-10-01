// Spec for the project page's pure model — runnable with Node's built-in
// runner after a tsc transpile. Covers reading the plan the server sends, how
// it groups under milestones, who may do what, and the words on each row.
import { test } from "node:test";
import assert from "node:assert/strict";

import { buildPlan, buildPlanData, gardenPlan, ORIGIN } from "./project-plan-fixtures";
import {
  askCandidates,
  askNote,
  asksForMe,
  assistantSeed,
  blockerChoices,
  blockersOf,
  canCompleteStep,
  canEditProject,
  canEditStep,
  crewLine,
  dayIso,
  dayLong,
  dayShort,
  dependentsOf,
  describeStep,
  doneLine,
  downstreamOf,
  findMember,
  findStep,
  groupLabel,
  groupStatus,
  healthWords,
  joinNames,
  leaveMessage,
  localToday,
  milestoneLine,
  ownerInitials,
  ownerLabel,
  parseDay,
  parsePlan,
  people,
  personLine,
  personStepState,
  personSteps,
  planGroups,
  PROJECT_TEMPLATES,
  projectRowLine,
  slipLine,
  stepFootnote,
  stepSpan,
  stepState,
  stepStatus,
  templateWrites,
  undatedOpenSteps,
  undatedPlanHint,
  undatedTimelineHint,
  upNext,
  whenLabel,
  whenLong,
  type PlanStep,
} from "./project-plan";

const TODAY = ORIGIN + 9;
const step = (plan: ReturnType<typeof gardenPlan>, id: string) => findStep(plan, id) as PlanStep;

test("days: ISO round-trips, labels read like a calendar", () => {
  const day = parseDay("2026-10-14") as number;
  assert.equal(dayIso(day), "2026-10-14");
  assert.equal(dayIso(day + 18), "2026-11-01");
  assert.equal(dayShort(day), "Oct 14");
  assert.equal(dayLong(day), "Wed, Oct 14");
  assert.equal(parseDay("nope"), null);
  assert.equal(parseDay(null), null);
  assert.equal(parseDay("2026-13-01"), null);
  assert.equal(parseDay("2026-10-05T09:00:00Z"), ORIGIN, "a timestamp reads as its calendar day");
  // The viewer's own calendar day, not UTC's.
  assert.equal(dayIso(localToday(new Date(2026, 9, 1, 23, 30))), "2026-10-01");
});

test("plan: decodes the garden and groups it under its milestones", () => {
  const plan = gardenPlan();
  assert.equal(plan.steps.length, 6);
  assert.deepEqual(planGroups(plan).map((g) => g.milestone?.id), ["plan", "beds", "plant"]);
  assert.deepEqual(blockersOf(plan, "plant").map((s) => s.id).sort(), ["frames", "seed"]);
  assert.deepEqual(dependentsOf(plan, "buy").map((s) => s.id), ["frames"]);
  assert.equal(plan.doneCount, 2);
  assert.equal(doneLine(plan), "2 of 6 steps done");
  assert.equal(doneLine({ doneCount: 0, total: 1 }), "0 of 1 step done");
});

test("plan: a payload without the viewer's membership isn't a plan", () => {
  assert.equal(parsePlan(null), null);
  assert.equal(parsePlan({ ...buildPlanData({ milestones: [], steps: [], edges: [] }), my_membership_id: "" }), null);
  // Missing optional fields fall back instead of throwing.
  const bare = parsePlan({ mission_id: "m", my_membership_id: "mb", title: "", members: [], milestones: [], steps: [], edges: [] });
  assert.equal(bare?.title, "Shared project");
  assert.equal(bare?.health, "on_track");
  assert.equal(bare?.myRole, "member");
  assert.equal(bare?.canInvite, false);
});

test("plan: steps with no milestone (or a removed one) go under Other steps, in order", () => {
  const plan = buildPlan({
    milestones: [["late", 30], ["early", 10]],
    steps: [
      { id: "z", milestone: "gone", start: 4, end: 5, title: "Zebra" },
      { id: "b", milestone: "early", start: 6, end: 7 },
      { id: "a", milestone: "early", start: 1, end: 2 },
      { id: "loose", start: null, end: null },
    ],
    edges: [],
  });
  const groups = planGroups(plan);
  // Milestones keep their own order (not date order): the plan's author decides.
  assert.deepEqual(groups.map((g) => g.id), ["late", "early", "unscheduled"]);
  assert.deepEqual(groups[1].steps.map((s) => s.id), ["b", "a"], "explicit order wins over dates");
  assert.deepEqual(groups[2].steps.map((s) => s.id), ["z", "loose"]);
  assert.equal(groupLabel(groups[0]), "M late · Nov 4");
  assert.equal(groupLabel(groups[2]), "Other steps");
});

test("plan: who has a step", () => {
  const plan = gardenPlan();
  assert.equal(ownerLabel(plan, step(plan, "frames")), "You + Sam");
  assert.equal(ownerLabel(plan, step(plan, "plant")), "Everyone");
  assert.equal(ownerLabel(plan, step(plan, "buy")), "Sam");
  assert.equal(ownerInitials(plan, step(plan, "frames")), "Y+S");
  assert.equal(ownerInitials(plan, step(plan, "plant")), "All");
  assert.equal(ownerInitials(plan, step(plan, "buy")), "S");

  const two = buildPlan({
    members: ["mb-you", "mb-kiho"],
    milestones: [],
    steps: [
      { id: "both", start: 0, end: 1, owners: ["mb-you", "mb-kiho"] },
      { id: "open", start: 0, end: 1, owners: [] },
      { id: "asked", start: 0, end: 1, asked: ["mb-kiho"] },
      { id: "mine", start: 0, end: 1, asked: ["mb-you"] },
    ],
    edges: [],
  });
  assert.equal(ownerLabel(two, step(two, "both")), "You + Kiho", "two people read better by name than as Everyone");
  assert.equal(ownerInitials(two, step(two, "both")), "Y+K");
  assert.equal(ownerLabel(two, step(two, "open")), "Anyone");
  assert.equal(ownerInitials(two, step(two, "open")), "?");
  assert.equal(ownerLabel(two, step(two, "asked")), "Asked Kiho");
  assert.equal(ownerLabel(two, step(two, "mine")), "Asked you");
  assert.deepEqual(asksForMe(two).map((s) => s.id), ["mine"]);
  assert.equal(crewLine(two), "You + Kiho");
  assert.equal(crewLine(gardenPlan()), "You + 2");
  assert.equal(crewLine(buildPlan({ members: ["mb-you"], milestones: [], steps: [], edges: [] })), "Just you");
});

test("plan: only owners complete; owners or the project owner edit", () => {
  const plan = gardenPlan();
  assert.ok(canCompleteStep(plan, step(plan, "frames")));
  assert.ok(!canCompleteStep(plan, step(plan, "buy")));
  assert.ok(canEditStep(plan, step(plan, "buy")), "I'm the project owner");
  assert.ok(canEditProject(plan));

  const member = buildPlan({ role: "member", milestones: [], steps: [{ id: "theirs", owners: ["mb-sam"] }, { id: "mine", owners: ["mb-you"] }], edges: [] });
  assert.ok(!canEditStep(member, step(member, "theirs")));
  assert.ok(canEditStep(member, step(member, "mine")));
  assert.ok(!canEditProject(member));
});

test("plan: when a step happens", () => {
  const plan = gardenPlan();
  assert.equal(whenLabel(step(plan, "buy")), "Oct 12 – 15", "the month isn't repeated");
  assert.equal(whenLong(step(plan, "buy")), "Mon, Oct 12 – Thu, Oct 15");
  const across = { start: parseDay("2026-10-30"), due: parseDay("2026-11-02") };
  assert.equal(whenLabel(across), "Oct 30 – Nov 2");
  assert.equal(whenLabel({ start: ORIGIN, due: ORIGIN }), "Oct 5");
  assert.equal(whenLabel({ start: null, due: ORIGIN + 3 }), "Oct 8", "one date is a one-day step");
  assert.equal(whenLabel({ start: null, due: null }), null);
  assert.deepEqual(stepSpan({ start: ORIGIN + 3, due: ORIGIN }), { start: ORIGIN, end: ORIGIN + 3 }, "a backwards range is drawn forwards");
});

test("step words: the one word at the end of a row", () => {
  const plan = gardenPlan();
  const word = (id: string, today = TODAY) => stepState(plan, step(plan, id), today);
  assert.deepEqual(word("measure"), { word: "done", tone: "done" });
  assert.deepEqual(word("buy"), { word: "in progress", tone: "ink" });
  assert.deepEqual(word("buy", ORIGIN + 20), { word: "overdue", tone: "attn" }, "under way but past its finish");
  assert.deepEqual(word("frames"), { word: "after 1 step", tone: "faint" });
  assert.deepEqual(word("plant"), { word: "after 2 steps", tone: "faint" });
  assert.deepEqual(word("seed"), { word: "this week", tone: "muted" });
  assert.deepEqual(word("seed", ORIGIN), { word: "not started", tone: "faint" });
  assert.deepEqual(word("seed", ORIGIN + 40), { word: "overdue", tone: "attn" });

  const asks = buildPlan({
    members: ["mb-you", "mb-kiho"],
    milestones: [],
    steps: [
      { id: "mine", start: 0, end: 30, asked: ["mb-you"] },
      { id: "theirs", start: 20, end: 30, asked: ["mb-kiho"] },
      { id: "undated", start: null, end: null },
      { id: "skipped", start: 0, end: 1, status: "skipped" },
    ],
    edges: [],
  });
  assert.deepEqual(stepState(asks, step(asks, "mine"), TODAY), { word: "asked you", tone: "attn" });
  assert.deepEqual(stepState(asks, step(asks, "theirs"), TODAY), { word: "waiting on Kiho", tone: "attn" });
  assert.deepEqual(stepState(asks, step(asks, "undated"), TODAY), { word: "no dates", tone: "faint" });
  assert.deepEqual(stepState(asks, step(asks, "skipped"), TODAY), { word: "skipped", tone: "faint" });
  assert.deepEqual(undatedOpenSteps(asks).map((s) => s.id), ["undated"]);
});

test("step words: the Status line on a step's own page", () => {
  const plan = gardenPlan();
  assert.equal(stepStatus(plan, step(plan, "measure")).word, "Done");
  assert.equal(stepStatus(plan, step(plan, "buy")).word, "In progress");
  assert.equal(stepStatus(plan, step(plan, "frames")).word, "Waiting");
  assert.equal(stepStatus(plan, step(plan, "seed")).word, "Not started");
  const open = buildPlan({ milestones: [], steps: [{ id: "x", owners: [] }], edges: [] });
  assert.deepEqual(stepStatus(open, step(open, "x")), { word: "Needs someone", tone: "attn" });
});

test("milestones: the word on a heading and the line in the strip", () => {
  const plan = gardenPlan();
  const [first, second] = planGroups(plan);
  assert.deepEqual(groupStatus(first), { word: "Reached", tone: "done" });
  assert.deepEqual(groupStatus(second), { word: "2 to go", tone: "faint" });
  assert.equal(milestoneLine(plan, plan.milestones[0]), "Oct 11 · reached");
  assert.equal(milestoneLine(plan, plan.milestones[1]), "Oct 25 · 0 of 2 done");
  const empty = buildPlan({ milestones: [["m", null]], steps: [], edges: [] });
  assert.deepEqual(groupStatus(planGroups(empty)[0]), { word: "No steps yet", tone: "faint" });
  assert.equal(milestoneLine(empty, empty.milestones[0]), "no steps yet");
  // Every step done but the server hasn't stamped it reached yet.
  const done = buildPlan({ milestones: [["m", 5]], steps: [{ id: "a", milestone: "m", status: "done" }], edges: [] });
  assert.equal(groupStatus({ ...planGroups(done)[0], milestone: { ...done.milestones[0], reached: false } }).word, "All done");
});

test("health: the words for on track, at risk and late", () => {
  assert.deepEqual(healthWords("on_track"), { short: "on track", long: "On track", tone: "done" });
  assert.deepEqual(healthWords("at_risk"), { short: "at risk", long: "At risk of slipping", tone: "attn" });
  assert.deepEqual(healthWords("late"), { short: "running late", long: "Running late", tone: "attn" });
});

test("neighborhood row: N of M done, my next step, and health only when it isn't fine", () => {
  const plan = gardenPlan();
  assert.equal(upNext(plan)?.id, "frames");
  assert.equal(projectRowLine(plan), "2 of 6 steps done · next: Step frames");
  assert.equal(projectRowLine({ ...plan, health: "late" }), "2 of 6 steps done · next: Step frames · running late");
  assert.equal(projectRowLine(buildPlan({ milestones: [], steps: [], edges: [] })), "No steps yet");
  // Nothing of mine left: the project's next open step stands in.
  const theirs = buildPlan({ milestones: [], steps: [{ id: "a", owners: ["mb-sam"], title: "Book the hall" }], edges: [] });
  assert.equal(upNext(theirs), undefined);
  assert.equal(projectRowLine(theirs), "0 of 1 step done · next: Book the hall");
});

test("people: each person's part and the line under their name", () => {
  const plan = gardenPlan();
  assert.deepEqual(people(plan).map((m) => m.id), ["mb-you", "mb-sam", "mb-rin"]);
  assert.deepEqual(personSteps(plan, "mb-sam").map((s) => s.id), ["buy", "frames", "plant"]);
  assert.equal(personLine(plan, findMember(plan, "mb-sam")!), "3 steps · 0 done · next: Step buy, Oct 12");
  assert.equal(personLine(plan, findMember(plan, "mb-rin")!), "3 steps · 1 done · next: Step seed, Oct 19");
  assert.equal(personStepState(plan, step(plan, "buy"), "mb-sam", TODAY).word, "in progress");
  assert.equal(personStepState(plan, step(plan, "frames"), "mb-sam", TODAY).word, "waiting");
  assert.equal(personStepState(plan, step(plan, "seed"), "mb-rin", ORIGIN).word, "starts Oct 19");
  assert.equal(personStepState(plan, step(plan, "measure"), "mb-rin", TODAY).word, "done");
  assert.equal(describeStep(plan, step(plan, "measure")), "Step measure (Rin, done)");
  assert.equal(describeStep(plan, step(plan, "buy")), "Step buy (Sam)");

  const invited = { ...plan, members: plan.members.map((m) => (m.id === "mb-rin" ? { ...m, status: "invited" } : m)) };
  assert.equal(personLine(invited, findMember(invited, "mb-rin")!), "Invited — they haven’t joined yet");
  const asked = buildPlan({ members: ["mb-you", "mb-kiho"], milestones: [], steps: [{ id: "a", start: 0, end: 20, asked: ["mb-kiho"] }], edges: [] });
  assert.deepEqual(personSteps(asked, "mb-kiho").map((s) => s.id), ["a"], "an unanswered ask is on their plate");
  assert.equal(personStepState(asked, step(asked, "a"), "mb-kiho", TODAY).word, "asked");
  assert.equal(personLine(asked, findMember(asked, "mb-you")!), "Nothing on your plate here yet");
});

test("dependencies: a step can't wait on anything that waits on it", () => {
  const plan = gardenPlan();
  assert.deepEqual([...downstreamOf(plan, "buy")].sort(), ["frames", "plant"]);
  assert.equal(downstreamOf(plan, "plant").size, 0);
  assert.deepEqual(blockerChoices(plan, "buy").map((s) => s.id), ["measure", "layout", "seed"]);
  assert.equal(blockerChoices(plan, null).length, 6, "a new step can wait on anything");
});

test("asking: who can still be asked, and what the note says", () => {
  const plan = gardenPlan();
  assert.deepEqual(askCandidates(plan, step(plan, "buy")).map((m) => m.id), ["mb-you", "mb-rin"]);
  assert.deepEqual(askCandidates(plan, step(plan, "plant")), []);
  assert.equal(askNote(plan, []), "Leave it open and anyone can take it.");
  assert.equal(askNote(plan, ["mb-you"]), "It goes on your own task list too.");
  assert.equal(askNote(plan, ["mb-you", "mb-sam", "mb-rin"]), "We’ll ask Sam and Rin first. It shows as “asked” until they say yes.");
  assert.equal(joinNames(["A", "B", "C"]), "A, B, and C");
  assert.equal(joinNames([]), "");
});

test("copy: undated steps, slipping, leaving, and who can finish a step", () => {
  const plan = gardenPlan();
  assert.equal(undatedPlanHint(plan), "");
  assert.equal(undatedTimelineHint(plan), "");
  const one = buildPlan({ milestones: [], steps: [{ id: "a", start: 0, end: 1 }, { id: "b", start: null, end: null }], edges: [] });
  assert.equal(undatedPlanHint(one), "1 step has no dates yet, so it isn’t on the timeline.");
  assert.equal(undatedTimelineHint(one), "1 step has no dates, so it isn’t shown.");
  const none = buildPlan({ milestones: [], steps: [{ id: "a", start: null, end: null }, { id: "b", start: null, end: null }], edges: [] });
  assert.equal(undatedPlanHint(none), "2 steps have no dates yet, so they aren’t on the timeline.");
  assert.equal(undatedTimelineHint(none), "Nothing has dates yet. Give steps a start and finish to see them here.");

  assert.equal(stepFootnote(plan, step(plan, "frames")), "It’s in your journal too. Only the people who took a step can mark it done.");
  assert.equal(stepFootnote(plan, step(plan, "buy")), "Only Sam can mark this done.");
  assert.equal(slipLine({ ...plan }, { ...step(plan, "buy"), movesIfLate: ["frames", "plant"] }), "If this slips, Step frames and Step plant move too.");
  assert.equal(slipLine(plan, { ...step(plan, "buy"), movesIfLate: ["frames"] }), "If this slips, Step frames moves too.");
  assert.equal(slipLine(plan, { ...step(plan, "measure"), movesIfLate: ["layout"] }), "", "a finished step can't slip");

  assert.match(leaveMessage(plan), /^You’re the owner — whoever joined first takes over\./);
  assert.match(leaveMessage({ ...plan, myRole: "member" }), /^You won’t see it anymore\./);
});

test("create: the request for your assistant, and what a template writes", () => {
  assert.equal(
    assistantSeed(" a street party ", "Everyone on the block comes", ["Kiho", "Dudley"]),
    "Help me plan a street party with Kiho and Dudley. The goal: Everyone on the block comes. Draft milestones, steps with rough dates, who could do each, and what waits on what.",
  );
  assert.equal(assistantSeed("a trip", "", []), "Help me plan a trip. Draft milestones, steps with rough dates, who could do each, and what waits on what.");

  const trip = PROJECT_TEMPLATES.find((t) => t.id === "trip")!;
  const writes = templateWrites(trip, ORIGIN);
  assert.deepEqual(writes.map((w) => w.kind), ["milestone", "step", "step", "milestone", "step", "dependency", "step", "milestone", "step", "dependency"]);
  assert.deepEqual(writes[0], { kind: "milestone", key: "m0", body: { title: "Plan agreed", target_date: "2026-10-12", order: 0 } });
  assert.deepEqual(writes[1], { kind: "step", key: "m0s0", milestoneKey: "m0", body: { title: "Pick dates", start_date: "2026-10-05", due_date: "2026-10-08", order: 0 } });
  // The last step of one milestone unlocks the first step of the next.
  assert.deepEqual(writes[5], { kind: "dependency", blockerKey: "m0s1", blockedKey: "m1s0" });
  assert.equal(PROJECT_TEMPLATES.length, 4);
  for (const t of PROJECT_TEMPLATES) {
    for (const w of templateWrites(t, ORIGIN)) if (w.kind === "step") assert.ok(w.body.start_date <= w.body.due_date && w.body.title.length <= 120);
  }
});
