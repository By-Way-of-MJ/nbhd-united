import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

// Same dependency-free TSX rendering approach as cardio-prescription.test.mjs.
const require = createRequire(import.meta.url);
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
function harness(flag, { linked = false, loading = false, statusesMissing = false, assistant = false } = {}) {
  const tenant = loading ? undefined : { telegram_available: flag, status: "active", user: {}, task_model_preferences: {} };
  const result = (data) => ({ data, isLoading: loading, isFetchedAfterMount: true, refetch() {} });
  const connection = { status: { linked, telegram_username: "yuki" }, statusReady: true, statusError: null, generationError: null, generate() {}, retryStatus() {} };
  const queries = new Proxy({
    useTenantQuery: () => result(tenant),
    useMeQuery: () => result({ tenant, telegram_chat_id: linked ? 123 : null, telegram_username: "yuki" }),
    useTelegramStatusQuery: () => result(statusesMissing ? undefined : { linked }),
    useLineStatusQuery: () => result(statusesMissing ? undefined : { linked: false }),
    usePushStatusQuery: () => result(statusesMissing ? undefined : { registered: false }),
    useProvisioningStatusQuery: () => result({ ready: true }),
    usePersonasQuery: () => result([]),
    useIntegrationsQuery: () => result([]),
    useWelcomeMessageQuery: () => result("Welcome home."),
  }, { get: (target, name) => target[name] ?? (() => ({ ...result(undefined), mutate() {}, reset() {} })) });
  const cache = new Map();
  function load(path) {
    const filename = [path, `${path}.tsx`, `${path}.ts`].find(existsSync);
    if (cache.has(filename)) return cache.get(filename);
    const js = ts.transpileModule(readFileSync(filename, "utf8"), {
      fileName: filename,
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
    }).outputText;
    const mod = { exports: {} };
    const localRequire = (name) => {
      if (name.endsWith(".css")) return {};
      if (name === "@tanstack/react-query") return { useQueryClient: () => ({ invalidateQueries() {} }) };
      if (name === "@/lib/queries") return queries;
      if (name === "@/components/channel/use-channel-connection") return { useTelegramConnection: () => connection, useLineConnection: () => ({ ...connection, status: { linked: false } }) };
      if (name === "next/navigation") return { useRouter: () => ({ push() {} }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/settings" };
      if (name === "next/link") return function TestLink({ children, ...props }) { return React.createElement("a", props, children); };
      if (name === "react") return { ...React, useSyncExternalStore: () => false, useState: (initial) => React.useState(assistant && initial === "blank" ? "assistant" : initial) };
      if (name.startsWith("@/")) return load(resolve(root, name.slice(2)));
      if (name.startsWith(".")) return load(resolve(dirname(filename), name));
      return require(name);
    };
    try { new Function("require", "module", "exports", js)(localRequire, mod, mod.exports); } catch (error) { error.message = `${filename}: ${error.message}`; throw error; }
    cache.set(filename, mod.exports);
    return mod.exports;
  }
  return (path, exported, props = {}) => renderToStaticMarkup(React.createElement(load(resolve(root, path))[exported], props));
}
const cardProps = { channel: "telegram", description: "Chat with your assistant in Telegram.", panelId: "pairing", linked: false, statusReady: true, statusError: null, pairingOpen: false, link: null, isGenerating: false, generationError: null, onConnect() {}, onRegenerate() {}, onRetryStatus() {}, onUnlink() {} };
for (const flag of [true, false, undefined]) {
  test(`channel scene and shared card: availability ${flag}`, () => {
    const render = harness(flag);
    const scene = render("components/onboarding/channel-scene", "ChannelScene", { onContinue() {} });
    const card = render("components/channel/messaging-channel-card", "MessagingChannelCard", cardProps);
    for (const html of [scene, card]) {
      if (flag === false) assert.doesNotMatch(html, /Telegram|fully supported/);
      else assert.match(html, /Connect Telegram/);
    }
    assert.match(scene, /Connect LINE/);
    assert.match(scene, /Set up later/);
    if (flag === false) assert.doesNotMatch(scene, /sm:grid-cols-2/);
  });
  test(`launch live and remembered outcome: availability ${flag}`, () => {
    for (const statusesMissing of [false, true]) {
      const html = harness(flag, { linked: true, statusesMissing })("components/onboarding/launch-sequence", "LaunchSequence", { outcome: "telegram" });
      if (flag === false) assert.doesNotMatch(html, /Open Telegram/);
      else assert.match(html, /Open Telegram/);
    }
  });
  test(`settings and integrations: availability ${flag}, linked and unlinked`, () => {
    for (const linked of [false, true]) {
      const render = harness(flag, { linked });
      for (const path of ["app/settings/page", "app/settings/integrations/page"]) {
        const html = render(path, "default");
        if (linked) {
          assert.match(html, /@yuki/);
          if (flag === false) assert.match(html, /Replies here are no longer available/);
        } else if (flag === false) assert.doesNotMatch(html, /Connect Telegram/);
        else assert.match(html, /Connect Telegram/);
        if (flag === false) assert.doesNotMatch(html, /Chat with your assistant in Telegram/);
      }
    }
  });
  test(`welcome: availability ${flag}`, () => {
    const html = harness(flag)("components/journal/welcome-message-card", "WelcomeMessageCard");
    assert.match(html, flag === false ? /Connect LINE/ : /Connect Telegram or LINE/);
  });
  test(`LINE pairing switch: availability ${flag}`, () => {
    const html = harness(flag)("components/channel/channel-pairing-panel", "ChannelPairingPanel", { ...cardProps, channel: "line", onSwitch() {} });
    if (flag === false) assert.doesNotMatch(html, /Telegram/);
    else assert.match(html, /Use Telegram instead/);
  });
}
test("loading never offers Telegram", () => {
  const render = harness(undefined, { loading: true, linked: true });
  assert.doesNotMatch(render("components/onboarding/channel-scene", "ChannelScene", { onContinue() {} }), /Telegram/);
  assert.doesNotMatch(render("components/onboarding/launch-sequence", "LaunchSequence", { outcome: "telegram" }), /Open Telegram/);
  assert.doesNotMatch(render("components/channel/messaging-channel-card", "MessagingChannelCard", cardProps), /Telegram/);
});
test("unavailable linked identity retains Unlink without an active badge or chat promise", () => {
  const html = harness(false)("components/channel/messaging-channel-card", "MessagingChannelCard", { ...cardProps, linked: true, connectedIdentity: "@yuki" });
  assert.match(html, /@yuki/);
  assert.match(html, /Unlink/);
  assert.match(html, /Replies here are no longer available/);
  assert.doesNotMatch(html, /Chat with|>active<|Connect Telegram|Generate a new link/);
});
test("already-open panel handles terminal 409 without a stale link or retry", () => {
  const html = harness(true)("components/channel/channel-pairing-panel", "ChannelPairingPanel", { ...cardProps, unavailable: true, generationError: "Telegram replies are no longer available. Use the iPhone app or LINE.", link: { deep_link: "https://t.me/stale", expires_at: "2099-01-01", qr_code: "stale.png" } });
  assert.match(html, /Telegram replies are no longer available/);
  assert.doesNotMatch(html, /t.me|stale.png|Generate a new link|Waiting for|Try again/);
});

for (const flag of [true, false, undefined]) {
  test(`project creation and Open Sky copy: availability ${flag}`, () => {
    const render = harness(flag, { assistant: true });
    const project = render("components/neighborhood/project/create", "CreateProjectV2Panel", { neighbors: [], onClose() {}, onCreated() {} });
    assert.match(project, /Send this to your assistant on iPhone or LINE/);
    assert.doesNotMatch(project, /Telegram/);
    const shell = render("components/open-sky/shell", "OpenSkyShell", { tenant: { telegram_available: flag }, onLogout() {} });
    assert.match(shell, /Talk to your assistant on iPhone or LINE/);
    assert.doesNotMatch(shell, /Telegram/);
  });
}
test("onboarding never counts a retired Telegram link as a connected outcome", () => {
  const html = harness(false, { linked: true })("components/onboarding/channel-scene", "ChannelScene", { onContinue() {} });
  assert.doesNotMatch(html, />Continue<|Telegram/);
  assert.match(html, /Set up later/);
});

test("a terminal 409 never re-offers connecting or claims chat works after the panel closes", () => {
  const html = harness(true)("components/channel/messaging-channel-card", "MessagingChannelCard", { ...cardProps, pairingOpen: false, unavailable: true, generationError: "Telegram replies are no longer available." });
  assert.match(html, /Telegram replies are no longer available/);
  assert.doesNotMatch(html, /Chat with|Connect Telegram|Generate a new link/);
});
