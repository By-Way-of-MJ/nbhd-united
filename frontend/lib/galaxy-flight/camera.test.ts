// Spec for the flight camera maths and key mapping. Pure functions only —
// runnable with Node's built-in runner after a tsc transpile.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  aheadZ,
  autoPose,
  buildAutoPath,
  easeInOut,
  focalFor,
  goalFor,
  HOVER_DISTANCE,
  isActivationKeyOnControl,
  isTypingTarget,
  keyAction,
  NEAR,
  nearestStar,
  onScreen,
  pickStar,
  project,
  projectLooped,
  STEER_ACTIONS,
} from "./camera";

const view = { w: 1000, h: 600, focal: focalFor(1000, 600) };
const cam = { x: 0, y: 0, z: 0 };

test("project puts the camera axis at the screen centre and scales by focal/dz", () => {
  const q = project({ x: 0, y: 0, z: 450 }, cam, view);
  assert.ok(q);
  assert.equal(q.x, 500);
  assert.equal(q.y, 300);
  assert.equal(q.f, view.focal / 450);
  const off = project({ x: 100, y: -50, z: 450 }, cam, view);
  assert.ok(off);
  assert.equal(off.x, 500 + 100 * q.f);
  assert.equal(off.y, 300 - 50 * q.f);
});

test("project returns null for anything at or behind the lens", () => {
  assert.equal(project({ x: 0, y: 0, z: NEAR - 1 }, cam, view), null);
  assert.equal(project({ x: 0, y: 0, z: -100 }, cam, view), null);
});

test("aheadZ loops the corridor so stars behind you reappear ahead", () => {
  assert.equal(aheadZ(300, 0, 5000), 300);
  assert.equal(aheadZ(300, 400, 5000), 5300);
  assert.equal(aheadZ(300, 5400, 5000), 5400 + 4900);
  const q = projectLooped({ x: 0, y: 0, z: 100 }, { x: 0, y: 0, z: 150 }, view, 5000);
  assert.ok(q);
  assert.equal(q.dz, 4950);
});

test("onScreen respects the margin", () => {
  assert.equal(onScreen({ x: -10, y: 300, f: 1, dz: 1 }, view, 40), true);
  assert.equal(onScreen({ x: -50, y: 300, f: 1, dz: 1 }, view, 40), false);
  assert.equal(onScreen({ x: 500, y: 700, f: 1, dz: 1 }, view, 0), false);
});

test("nearestStar picks the closest star that is actually on screen", () => {
  const stars = [
    { x: 0, y: 0, z: 2000 },
    { x: 0, y: 0, z: 800 },
    { x: 9000, y: 0, z: 300 }, // closest, but far off to the side
  ];
  assert.equal(nearestStar(stars, cam, view, 5000), 1);
  assert.equal(nearestStar([], cam, view, 5000), null);
  // A floor ignores stars already sliding past the lens.
  assert.equal(nearestStar([{ x: 0, y: 0, z: 60 }, { x: 0, y: 0, z: 500 }], cam, view, 5000, 140), 1);
});

test("easeInOut is a smoothstep clamped to 0..1", () => {
  assert.equal(easeInOut(-1), 0);
  assert.equal(easeInOut(0), 0);
  assert.equal(easeInOut(0.5), 0.5);
  assert.equal(easeInOut(1), 1);
  assert.equal(easeInOut(2), 1);
  assert.ok(easeInOut(0.25) < 0.25 && easeInOut(0.75) > 0.75);
});

test("autopilot path starts at the camera, ends at the goal, and swings out sideways in between", () => {
  const from = { x: 0, y: 0, z: 0 }, to = { x: 0, y: 0, z: 2000 };
  const p = buildAutoPath(from, to, 10, { bulge: 0.3, minDur: 1, maxDur: 5, seed: 4 });
  assert.deepEqual(autoPose(p, 10).pose, from);
  const end = autoPose(p, 10 + p.dur);
  assert.deepEqual(end.pose, to);
  assert.equal(end.done, true);
  const mid = autoPose(p, 10 + p.dur / 2);
  assert.equal(mid.done, false);
  assert.ok(Math.abs(mid.pose.x) + Math.abs(mid.pose.y) > 100, "no sideways swing at the midpoint");
  assert.ok(Math.abs(mid.pose.z - 1000) < 1e-6);
  // Duration scales with distance, within the clamp.
  assert.ok(p.dur >= 1 && p.dur <= 5);
  assert.equal(buildAutoPath(from, { x: 0, y: 0, z: 50 }, 0, { bulge: 0.3, minDur: 1, maxDur: 5 }).dur, 1);
  assert.equal(buildAutoPath(from, { x: 0, y: 0, z: 90000 }, 0, { bulge: 0.3, minDur: 1, maxDur: 5 }).dur, 5);
});

test("autopilot slows as it arrives (the last quarter covers less distance than the middle)", () => {
  const p = buildAutoPath({ x: 0, y: 0, z: 0 }, { x: 0, y: 0, z: 4000 }, 0, { bulge: 0, minDur: 2, maxDur: 2, seed: 1 });
  const z = (u: number) => autoPose(p, u * p.dur).pose.z;
  const middle = z(0.625) - z(0.375), last = z(1) - z(0.75);
  assert.ok(last < middle, `last quarter ${last} vs middle ${middle}`);
  assert.equal(z(0.5), 2000);
});

test("a straight-in path (bulge 0) never leaves the line", () => {
  const p = buildAutoPath({ x: 10, y: 20, z: 0 }, { x: 10, y: 20, z: 500 }, 0, { bulge: 0, minDur: 1, maxDur: 1 });
  for (const u of [0.1, 0.5, 0.9]) {
    const q = autoPose(p, u).pose;
    assert.equal(q.x, 10);
    assert.equal(q.y, 20);
  }
});

test("pickStar returns the star under the pointer within the radius, or null", () => {
  const stars = [
    { x: 0, y: 0, z: 450 }, // projects to (500,300)
    { x: 200, y: 0, z: 450 },
  ];
  assert.equal(pickStar(stars, cam, view, 5000, 510, 305), 0);
  assert.equal(pickStar(stars, cam, view, 5000, 500, 100), null);
  const f = view.focal / 450;
  assert.equal(pickStar(stars, cam, view, 5000, 500 + 200 * f, 300), 1);
});

test("goalFor parks in front of the star, backing up when it is just behind", () => {
  const g = goalFor({ x: 10, y: 20, z: 1000 }, cam, 5000);
  assert.deepEqual(g, { x: 10, y: 20, z: 1000 - HOVER_DISTANCE });
  const behind = goalFor({ x: 0, y: 0, z: 900 }, { x: 0, y: 0, z: 1000 }, 5000);
  assert.equal(behind.z, 900 - HOVER_DISTANCE);
  const wrapped = goalFor({ x: 0, y: 0, z: 100 }, { x: 0, y: 0, z: 4900 }, 5000);
  assert.equal(wrapped.z, 5100 - HOVER_DISTANCE);
});

test("keyAction maps arrows, WASD (any case), Space, E, M and Escape", () => {
  assert.equal(keyAction("ArrowLeft"), "left");
  assert.equal(keyAction("A"), "left");
  assert.equal(keyAction("d"), "right");
  assert.equal(keyAction("ArrowUp"), "up");
  assert.equal(keyAction("ArrowDown"), "down");
  assert.equal(keyAction("W"), "faster");
  assert.equal(keyAction("s"), "slower");
  assert.equal(keyAction(" "), "stop");
  assert.equal(keyAction("E"), "land");
  assert.equal(keyAction("m"), "map");
  assert.equal(keyAction("Escape"), "exit");
  assert.equal(keyAction("x"), null);
  assert.equal(keyAction(""), null);
  for (const a of ["left", "right", "up", "down", "faster", "slower"] as const) assert.ok(STEER_ACTIONS.has(a));
  assert.equal(STEER_ACTIONS.has("stop"), false);
});

test("typing in a field is never hijacked; Space/Enter on a button stays a click", () => {
  assert.equal(isTypingTarget("input"), true);
  assert.equal(isTypingTarget("TEXTAREA"), true);
  assert.equal(isTypingTarget("div", true), true);
  assert.equal(isTypingTarget("div"), false);
  assert.equal(isTypingTarget("button"), false);
  assert.equal(isActivationKeyOnControl("BUTTON", " "), true);
  assert.equal(isActivationKeyOnControl("a", "Enter"), true);
  assert.equal(isActivationKeyOnControl("BUTTON", "m"), false);
  assert.equal(isActivationKeyOnControl("DIV", " "), false);
});
