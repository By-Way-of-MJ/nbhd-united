// Run against NEXT_PUBLIC_WEB_FIXTURES=1 npm run dev. Uses an existing local
// Playwright install; never downloads packages or browsers. Set PLAYWRIGHT_MODULE,
// optional CHROMIUM_PATH, and SCREENSHOT_DIR to use existing local tooling.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const base = process.env.FIXTURE_URL || "http://127.0.0.1:3217";
const output = process.env.SCREENSHOT_DIR;
if (!output) throw new Error("Set SCREENSHOT_DIR outside the repository.");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) });
const errors = [];
async function visit(path, fixture, width = 1440) {
  const context = await browser.newContext({ viewport: { width, height: 1100 }, reducedMotion: "reduce" });
  await context.route("**/*", (route) => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
  await context.addInitScript(() => {
    localStorage.clear();
    localStorage.setItem("nbhd_access_token", "local-fixture-only");
  });
  const page = await context.newPage();
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${base}${path}?fixture=${fixture}`);
  if (path === "/settings") await page.getByRole("link", { name: "Connect LINE", exact: true }).waitFor();
  else await page.getByRole("button", { name: "Connect LINE", exact: true }).waitFor();
  return { page, context };
}
async function shot(page, name) {
  await page.screenshot({ path: `${output}/${name}.png`, fullPage: true, style: "nextjs-portal { display: none; }" });
  console.log(`PASS ${name}.png`);
}
try {
  for (const width of [1440, 390]) {
    const suffix = width === 390 ? "-mobile" : "";
    for (const mode of ["tg-off", "tg-on", "tg-absent"]) {
      const { page, context } = await visit("/onboarding", mode, width);
      assert.equal(await page.getByRole("button", { name: "Connect Telegram", exact: true }).count(), mode === "tg-off" ? 0 : 1);
      if (mode === "tg-off") assert.doesNotMatch(await page.locator("body").innerText(), /Telegram|fully supported/);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      await shot(page, `onboarding-${mode}${suffix}`);
      if (mode === "tg-off" && width === 1440) {
        await page.getByRole("button", { name: "Connect LINE", exact: true }).click();
        await page.getByText("Waiting for LINE…", { exact: true }).waitFor();
        assert.doesNotMatch(await page.locator("body").innerText(), /Telegram/);
        await shot(page, "onboarding-line-pairing-tg-off");
      }
      await page.getByRole("button", { name: "Set up later", exact: true }).click();
      await page.getByRole("button", { name: "Start exploring", exact: true }).waitFor();
      assert.doesNotMatch(await page.locator("body").innerText(), /Open Telegram/);
      if (mode === "tg-off") await shot(page, `launch-tg-off${suffix}`);
      await context.close();
    }
  }
  for (const mode of ["tg-off", "tg-off-linked", "tg-on", "tg-absent"]) {
    const { page, context } = await visit("/settings/integrations", mode);
    const linked = mode.endsWith("-linked");
    assert.equal(await page.getByRole("button", { name: "Connect Telegram", exact: true }).count(), mode.startsWith("tg-off") ? 0 : 1);
    if (linked) {
      await page.getByText("Telegram connected as @yuki_fixture", { exact: true }).waitFor();
      assert.match(await page.locator("body").innerText(), /Replies here are no longer available/);
      assert.doesNotMatch(await page.locator("body").innerText(), /Chat with your assistant in Telegram/);
    }
    await shot(page, `integrations-${mode}`);
    if (linked) {
      await page.getByRole("button", { name: "Unlink", exact: true }).click();
      assert.match(await page.locator("body").innerText(), /This removes your linked Telegram identity/);
      await page.getByRole("button", { name: "Yes, unlink Telegram", exact: true }).click();
      await page.getByText("Telegram connected as @yuki_fixture", { exact: true }).waitFor({ state: "hidden" });
      assert.doesNotMatch(await page.locator("body").innerText(), /Connect Telegram/);
    }
    await context.close();
    const settings = await visit("/settings", mode);
    assert.equal(await settings.page.getByRole("link", { name: "Connect Telegram", exact: true }).count(), mode.startsWith("tg-off") ? 0 : 1);
    if (linked) assert.match(await settings.page.locator("body").innerText(), /@yuki_fixture/);
    await shot(settings.page, `settings-${mode}`);
    await settings.context.close();
  }
  for (const mode of ["tg-409", "tg-status-409"]) {
    const { page, context } = await visit("/settings/integrations", mode);
    await page.getByRole("button", { name: "Connect Telegram", exact: true }).click();
    await page.getByText("Waiting for Telegram…", { exact: true }).waitFor();
    if (mode === "tg-409") await page.getByRole("button", { name: "Generate a new link", exact: true }).click();
    await page.getByText("Telegram replies are no longer available. Use the iPhone app or LINE.", { exact: true }).waitFor();
    assert.equal(await page.getByRole("link", { name: "Open in Telegram", exact: true }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "Generate a new link", exact: true }).count(), 0);
    assert.doesNotMatch(await page.locator("body").innerText(), /Chat with your assistant in Telegram/);
    await shot(page, `integrations-${mode}`);
    await context.close();
  }
  // Existing auth-dependent SSR renders logged-out markup before fixture auth
  // loads on the client (OnboardingPage/SettingsPage). Keep the warning visible
  // in output; reject every other browser exception.
  const hydration = errors.filter((message) => message.startsWith("Hydration failed because the server rendered HTML"));
  if (hydration.length) console.log(`KNOWN auth/fixture hydration warnings: ${hydration.length}`);
  assert.deepEqual(errors.filter((message) => !hydration.includes(message)), []);
  console.log("PASS browser assertions, including unavailable generation/status and unlink");
} finally {
  await browser.close();
}
