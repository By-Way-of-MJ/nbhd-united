// Spec for the flight camera maths and key mapping. Pure functions only —
// runnable with Node's built-in runner after a tsc transpile.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  aheadZ,
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
