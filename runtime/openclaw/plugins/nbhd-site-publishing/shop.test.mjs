import test, { after } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";

import shopLib from "./shop.js";

const { createShop, parseYen, parseStock, slugify, formatYen, sniffImageExtension, APPROVAL_TTL_MS, MAX_IMAGES } =
  shopLib;
const require = createRequire(import.meta.url);

const CONFIG = {
  cosmosEndpoint: "https://example.invalid:443/",
  cosmosDatabase: "KihokoPortfolio",
  cosmosContainer: "Portfolio",
  blobAccount: "exampleblob",
  blobContainer: "media",
};
const JPEG = Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0]), Buffer.from("fake jpeg body")]);
const PNG = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.from("png")]);
const TEST_ROOT = mkdtempSync(join(tmpdir(), "nbhd-shop-tests-"));
let sequence = 0;

after(() => rmSync(TEST_ROOT, { recursive: true, force: true }));

function photo(name, bytes = JPEG) {
  const dir = mkdtempSync(join(TEST_ROOT, "photos-"));
  const file = join(dir, name);
  writeFileSync(file, bytes);
  return file;
}

function codedError(code, message = `status ${code}`) {
  return Object.assign(new Error(message), { code });
}

/** In-memory stand-in for the Cosmos container + blob container. No network. */
function fakeStore(docs = []) {
  const items = new Map(docs.map((doc) => [`${doc.type}/${doc.id}`, { _etag: "etag-1", ...doc }]));
  const blobs = new Map();
  const log = [];
  const fail = {};
  const products = () => [...items.values()].filter((doc) => doc.type === "product");
  const container = {
    items: {
      query(spec) {
        const params = Object.fromEntries(spec.parameters.map((p) => [p.name, p.value]));
        // Every shop query must stay inside the product partition.
        assert.equal(params["@type"], "product", spec.query);
        assert.match(spec.query, /c\.type = @type/);
        log.push(["query", spec.query]);
        return {
          async fetchAll() {
            if (fail.query) throw fail.query;
            if (spec.query.includes("c.slug = @slug")) {
              return { resources: products().filter((p) => p.slug === params["@slug"]).map((p) => ({ id: p.id })) };
            }
            if (spec.query.includes("c.id = @v OR c.slug = @v")) {
              return { resources: products().filter((p) => p.id === params["@v"] || p.slug === params["@v"]) };
            }
            return {
              resources: products().map(({ id, slug, title, priceJpy, stock, status }) => ({
                id,
                slug,
                title,
                priceJpy,
                stock,
                status,
              })),
            };
          },
        };
      },
      async create(doc) {
        log.push(["create", doc.id]);
        if (fail.create) throw fail.create;
        const key = `${doc.type}/${doc.id}`;
        if (items.has(key)) throw codedError(409);
        items.set(key, { ...doc, _etag: "etag-1" });
        return { resource: doc };
      },
    },
    item(id, partitionKey) {
      const key = `${partitionKey}/${id}`;
      return {
        async read() {
          log.push(["read", key]);
          return { resource: items.has(key) ? structuredClone(items.get(key)) : undefined };
        },
        async replace(doc, options) {
          log.push(["replace", key, options]);
          if (fail.replace) throw fail.replace;
          const current = items.get(key);
          if (options?.accessCondition && options.accessCondition.condition !== current._etag) throw codedError(412);
          items.set(key, { ...doc, _etag: `${current._etag}+` });
          return { resource: doc };
        },
      };
    },
  };
  const blobContainer = {
    getBlockBlobClient(name) {
      return {
        async uploadData(buffer, options) {
          log.push(["upload", name]);
          if (fail.upload) throw fail.upload;
          blobs.set(name, { buffer, options });
        },
        async deleteIfExists() {
          log.push(["deleteBlob", name]);
          blobs.delete(name);
        },
      };
    },
  };
  return { container, blobContainer, items, blobs, log, fail, products };
}

function harness({ docs = [], config = CONFIG, startAt = "2026-10-06T03:00:00.000Z" } = {}) {
  const store = fakeStore(docs);
  const clock = { value: new Date(startAt) };
  let uuid = 0;
  let opened = 0;
  const stateDir = join(TEST_ROOT, `state-${++sequence}`);
  const make = () =>
    createShop({
      config,
      env: {},
      now: () => clock.value,
      randomUUID: () => `00000000-0000-4000-8000-${String(++uuid).padStart(12, "0")}`,
      openStore: () => {
        opened += 1;
        return store;
      },
      stateDir,
    });
  const shop = make();
  return { shop, store, clock, stateDir, restart: make, opened: () => opened };
}

const text = (result) => result.content[0].text;
const codeFrom = (result) => {
  const match = text(result).match(/Approval code: ([A-Z2-9]{6})\./);
  assert.ok(match, `no approval code in: ${text(result)}`);
  return match[1];
};
const writes = (store) => store.log.filter(([kind]) => ["create", "replace", "upload"].includes(kind));

function product(overrides = {}) {
  return {
    id: "p-1",
    type: "product",
    title: "Cherry Blossom Print",
    slug: "cherry-blossom-print",
    description: "A4 print",
    priceJpy: 8000,
    productType: "print",
    stock: 3,
    images: [{ blobName: "images/a.jpg", thumbnailBlobName: "thumbnails/a.jpg" }],
    status: "active",
    order: 0,
    createdAt: "2026-09-01T00:00:00.000Z",
    updatedAt: "2026-09-01T00:00:00.000Z",
    ...overrides,
  };
}

// ---------- price / stock / slug parsing ----------

test("parseYen accepts the forms manage-store.js documents, with the same result", () => {
  assert.equal(parseYen("12000"), 12000);
  assert.equal(parseYen("¥12,000"), 12000);
  assert.equal(parseYen("12,000 yen"), 12000);
  assert.equal(parseYen(" ￥８，０００ "), 8000);
  assert.equal(parseYen("8000円"), 8000);
  assert.equal(parseYen("JPY 1"), 1);
  assert.equal(parseYen(15000), 15000);
});

test("parseYen refuses anything that isn't an unambiguous whole number of yen", () => {
  for (const bad of ["", "0", "-500", "8,000.00", "80.5", "12k", "1.2万", "8000 or 9000", "cheap", "1,00", null, 0, 8.5, NaN, "100000000"]) {
    assert.throws(() => parseYen(bad), /price/, String(bad));
  }
});

test("parseStock handles numbers, unlimited, and rejects the rest", () => {
  assert.equal(parseStock("10"), 10);
  assert.equal(parseStock(0), 0);
  assert.equal(parseStock("Unlimited"), null);
  for (const bad of ["-1", "2.5", "a few", 1.5, -3, "", "1000000"]) {
    assert.throws(() => parseStock(bad), /Stock must be/, String(bad));
  }
});

test("slugify matches the script, including the fallback for non-ASCII names", () => {
  assert.equal(slugify("Cherry Blossom Print"), "cherry-blossom-print");
  assert.equal(slugify("  Mt. Fuji — No.2!  "), "mt-fuji-no2");
  assert.equal(slugify("桜の版画", () => "abcdef12-0000"), "item-abcdef12");
});

test("formatYen groups thousands", () => {
  assert.equal(formatYen(8000), "¥8,000");
  assert.equal(formatYen(1250000), "¥1,250,000");
  assert.equal(formatYen(500), "¥500");
});

test("sniffImageExtension recognises only real images", () => {
  assert.equal(sniffImageExtension(JPEG), ".jpg");
  assert.equal(sniffImageExtension(PNG), ".png");
  assert.equal(sniffImageExtension(Buffer.from("GIF89a....")), ".gif");
  assert.equal(sniffImageExtension(Buffer.from("RIFF\0\0\0\0WEBPVP8 ")), ".webp");
  assert.equal(sniffImageExtension(Buffer.from("#!/bin/sh\necho hi")), null);
});

// ---------- approval gate ----------

test("no change tool writes anything until shop_confirm is called with the code", async () => {
  const { shop, store } = harness({ docs: [product()] });
  const staged = [
    await shop.tools.shop_add_item({ title: "New Bowl", price_jpy: "4500", image_paths: [photo("bowl.jpg")] }),
    await shop.tools.shop_update_item({ item: "cherry-blossom-print", price_jpy: "9000" }),
  ];
  for (const result of staged) {
    assert.match(text(result), /Nothing has changed on the site yet\. Approval code: [A-Z2-9]{6}\./);
  }
  assert.deepEqual(writes(store), []);
  assert.equal(store.blobs.size, 0);
  assert.equal(store.items.get("product/p-1").priceJpy, 8000);
});

test("shop_confirm refuses a missing, wrong, or already-used code", async () => {
  const { shop, store } = harness({ docs: [product()] });
  assert.match(text(await shop.tools.shop_confirm({})), /approval code .* is required/);
  assert.match(text(await shop.tools.shop_confirm({ approval_code: "ZZZZZZ" })), /doesn't match a waiting change/);

  const code = codeFrom(await shop.tools.shop_update_item({ item: "p-1", price_jpy: "9000" }));
  assert.match(text(await shop.tools.shop_confirm({ approval_code: ` ${code.toLowerCase()} ` })), /Updated/);
  assert.equal(store.items.get("product/p-1").priceJpy, 9000);
  assert.match(text(await shop.tools.shop_confirm({ approval_code: code })), /doesn't match a waiting change/);
  assert.equal(writes(store).length, 1);
});

test("an approval code expires after 24 hours", async () => {
  const { shop, store, clock } = harness({ docs: [product()] });
  const code = codeFrom(await shop.tools.shop_mark_sold({ item: "p-1" }));
  clock.value = new Date(clock.value.getTime() + APPROVAL_TTL_MS + 1000);
  assert.match(text(await shop.tools.shop_confirm({ approval_code: code })), /doesn't match a waiting change/);
  assert.deepEqual(writes(store), []);
});

test("re-staging a change to the same item retires the earlier code", async () => {
  const { shop, store } = harness({ docs: [product()] });
  const first = codeFrom(await shop.tools.shop_update_item({ item: "p-1", price_jpy: "90000" }));
  const second = codeFrom(await shop.tools.shop_update_item({ item: "p-1", price_jpy: "9000" }));
  assert.notEqual(first, second);
  assert.match(text(await shop.tools.shop_confirm({ approval_code: first })), /doesn't match a waiting change/);
  assert.equal(store.items.get("product/p-1").priceJpy, 8000);
  await shop.tools.shop_confirm({ approval_code: second });
  assert.equal(store.items.get("product/p-1").priceJpy, 9000);
});

test("a staged change survives a restart and is published exactly as summarised", async () => {
  const { shop, store, restart, stateDir } = harness();
  const file = photo("vase.jpg");
  const code = codeFrom(await shop.tools.shop_add_item({ title: "Blue Vase", price_jpy: "¥12,000", image_paths: [file] }));
  assert.ok(existsSync(join(stateDir, "shop-pending.json")));

  const afterRestart = restart();
  assert.match(text(await afterRestart.tools.shop_confirm({ approval_code: code })), /Added "Blue Vase" to the shop: ¥12,000/);
  assert.equal(store.products()[0].priceJpy, 12000);
  assert.ok(!existsSync(join(stateDir, "shop-pending.json")));
});

test("a corrupt pending file is treated as no waiting changes", async () => {
  const { shop, stateDir, store } = harness({ docs: [product()] });
  await shop.tools.shop_mark_sold({ item: "p-1" });
  writeFileSync(join(stateDir, "shop-pending.json"), "{not json");
  assert.match(text(await shop.tools.shop_confirm({ approval_code: "ABCDEF" })), /doesn't match a waiting change/);
  assert.deepEqual(writes(store), []);
});

// ---------- adding items ----------

test("shop_add_item writes the same document shape as manage-store.js", async () => {
  const { shop, store } = harness({
    docs: [{ id: "g-1", type: "image", title: "Sakura", blobName: "projects/portfolio/g-1.jpg", thumbnailBlobName: "" }],
  });
  const first = photo("print-front.jpg");
  const second = photo("print-back.PNG", PNG);
  const staged = await shop.tools.shop_add_item({
    title: "  Cherry Blossom Print ",
    price_jpy: "8,000 yen",
    image_paths: [first, second],
    gallery_image_ids: ["g-1"],
    stock: "10",
    product_type: "Print",
    description: "A4 giclee print on archival paper.",
  });
  const summary = text(staged);
  for (const expected of [
    "Name: Cherry Blossom Print",
    "Price: ¥8,000",
    "Stock: 10 in stock",
    "Type: print",
    'Photos: 2 new (print-front.jpg, print-back.PNG) + 1 from the gallery ("Sakura")',
    "Page: /shop/item/cherry-blossom-print",
  ]) {
    assert.ok(summary.includes(expected), `${expected}\n---\n${summary}`);
  }

  const done = text(await shop.tools.shop_confirm({ approval_code: codeFrom(staged) }));
  assert.equal(
    done,
    'Added "Cherry Blossom Print" to the shop: ¥8,000, 10 in stock. Page: /shop/item/cherry-blossom-print. ' +
      "It should appear on the site within a minute.",
  );

  const [doc] = store.products();
  assert.deepEqual(Object.keys(doc).filter((key) => key !== "_etag"), [
    "id",
    "type",
    "title",
    "slug",
    "description",
    "priceJpy",
    "productType",
    "stock",
    "images",
    "status",
    "order",
    "createdAt",
    "updatedAt",
  ]);
  const [imageA, imageB] = doc.images;
  assert.match(imageA.blobName, /^images\/[0-9a-f-]{36}\.jpg$/);
  assert.match(imageB.blobName, /^images\/[0-9a-f-]{36}\.png$/);
  assert.deepEqual({ ...doc, _etag: undefined }, {
    _etag: undefined,
    id: doc.id,
    type: "product",
    title: "Cherry Blossom Print",
    slug: "cherry-blossom-print",
    description: "A4 giclee print on archival paper.",
    priceJpy: 8000,
    productType: "print",
    stock: 10,
    images: [
      // No sharp in the runtime: the full image is its own thumbnail (script fallback).
      { blobName: imageA.blobName, thumbnailBlobName: imageA.blobName },
      { blobName: imageB.blobName, thumbnailBlobName: imageB.blobName },
      { imageId: "g-1", blobName: "projects/portfolio/g-1.jpg", thumbnailBlobName: "projects/portfolio/g-1.jpg" },
    ],
    status: "active",
    order: 0,
    createdAt: "2026-10-06T03:00:00.000Z",
    updatedAt: "2026-10-06T03:00:00.000Z",
  });
  assert.match(doc.id, /^[0-9a-f-]{36}$/);

  assert.deepEqual([...store.blobs.keys()], [imageA.blobName, imageB.blobName]);
  assert.deepEqual(store.blobs.get(imageA.blobName).options, {
    blobHTTPHeaders: { blobContentType: "image/jpeg", blobCacheControl: "public, max-age=31536000" },
  });
  assert.equal(store.blobs.get(imageB.blobName).options.blobHTTPHeaders.blobContentType, "image/png");
  assert.deepEqual(store.blobs.get(imageA.blobName).buffer, readFileSync(first));
});

test("stock, one-of-a-kind and hidden decide the starting status", async () => {
  const cases = [
    [{}, null, "active"],
    [{ stock: "unlimited" }, null, "active"],
    [{ one_of_a_kind: true }, 1, "active"],
    [{ one_of_a_kind: true, stock: "1" }, 1, "active"],
    [{ stock: "0" }, 0, "sold"],
    [{ stock: "5", hidden: true }, 5, "hidden"],
    [{ stock: "0", hidden: true }, 0, "hidden"],
  ];
  for (const [extra, stock, status] of cases) {
    const { shop, store } = harness();
    const staged = await shop.tools.shop_add_item({ title: "Bowl", price_jpy: "3000", image_paths: [photo("b.jpg")], ...extra });
    await shop.tools.shop_confirm({ approval_code: codeFrom(staged) });
    const [doc] = store.products();
    assert.equal(doc.stock, stock, JSON.stringify(extra));
    assert.equal(doc.status, status, JSON.stringify(extra));
    assert.equal(doc.productType, "art");
    assert.equal(doc.description, "");
  }
});

test("a taken slug gets -2, -3 … like the script", async () => {
  const { shop, store } = harness({
    docs: [product({ id: "a", slug: "blue-vase" }), product({ id: "b", slug: "blue-vase-2" })],
  });
  const staged = await shop.tools.shop_add_item({ title: "Blue Vase", price_jpy: "5000", image_paths: [photo("v.jpg")] });
  assert.match(text(await shop.tools.shop_confirm({ approval_code: codeFrom(staged) })), /Page: \/shop\/item\/blue-vase-3\./);
  assert.equal(store.products().find((p) => !["a", "b"].includes(p.id)).slug, "blue-vase-3");
});

test("a Japanese name gets the script's item-<id> page name", async () => {
  const { shop, store } = harness();
  const staged = await shop.tools.shop_add_item({ title: "桜の版画", price_jpy: "8000円", image_paths: [photo("s.jpg")] });
  await shop.tools.shop_confirm({ approval_code: codeFrom(staged) });
  const [doc] = store.products();
  assert.equal(doc.title, "桜の版画");
  assert.match(doc.slug, /^item-[0-9a-f]{8}$/);
});

test("every input problem is refused before any upload, lookup result or staged change", async () => {
  const good = photo("good.jpg");
  const notAnImage = photo("script.jpg", Buffer.from("#!/bin/sh\nrm -rf /\n"));
  const mislabelled = photo("really-png.jpg", PNG);
  const wrongType = photo("notes.pdf", Buffer.from("%PDF-1.7"));
  const empty = photo("empty.png", Buffer.alloc(0));
  const base = { title: "Bowl", price_jpy: "3000", image_paths: [good] };
  const cases = [
    [{ ...base, title: undefined }, /needs a name/],
    [{ ...base, title: "   " }, /needs a name/],
    [{ ...base, title: "x".repeat(121) }, /at most 120 characters/],
    [{ ...base, price_jpy: undefined }, /needs a price in yen/],
    [{ ...base, price_jpy: "about 3000" }, /not a valid price in yen/],
    [{ ...base, price_jpy: "30.50" }, /not a valid price in yen/],
    [{ ...base, stock: "lots" }, /Stock must be/],
    [{ ...base, one_of_a_kind: true, stock: "4" }, /exactly 1 in stock/],
    [{ ...base, product_type: "nft" }, /must be one of: art, original, print, merch/],
    [{ ...base, image_paths: [] }, /at least one photo/],
    [{ ...base, image_paths: undefined }, /at least one photo/],
    [{ ...base, image_paths: "photo.jpg" }, /must be a list/],
    [{ ...base, image_paths: [good, good] }, /listed twice/],
    [{ ...base, image_paths: [good, "/nope/missing.jpg"] }, /Photo not found: missing\.jpg/],
    [{ ...base, image_paths: [good, wrongType] }, /Unsupported photo type "\.pdf"/],
    [{ ...base, image_paths: [good, notAnImage] }, /not a valid jpg image/],
    [{ ...base, image_paths: [good, mislabelled] }, /not a valid jpg image/],
    [{ ...base, image_paths: [good, empty] }, /is empty/],
    [{ ...base, image_paths: Array.from({ length: MAX_IMAGES + 1 }, (_, i) => photo(`p${i}.jpg`)) }, /at most 8 photos/],
    [{ ...base, gallery_image_ids: ["missing-gallery-id"] }, /No gallery image found with id "missing-gallery-id"/],
  ];
  for (const [params, expected] of cases) {
    const { shop, store, stateDir } = harness();
    const result = text(await shop.tools.shop_add_item(params));
    assert.match(result, expected);
    assert.match(result, /^Couldn't prepare the shop item: /);
    assert.doesNotMatch(result, /Approval code/);
    assert.deepEqual(writes(store), [], result);
    assert.equal(store.blobs.size, 0);
    assert.ok(!existsSync(join(stateDir, "shop-pending.json")), result);
  }
});

test("a photo that vanished or changed after approval blocks the publish before any upload", async () => {
  for (const mutate of [(file) => rmSync(file), (file) => writeFileSync(file, Buffer.concat([JPEG, Buffer.from("x")]))]) {
    const { shop, store } = harness();
    const keep = photo("keep.jpg");
    const file = photo("gone.jpg");
    const code = codeFrom(await shop.tools.shop_add_item({ title: "Bowl", price_jpy: "3000", image_paths: [keep, file] }));
    mutate(file);
    assert.match(text(await shop.tools.shop_confirm({ approval_code: code })), /Photo not found: gone\.jpg|changed after it was approved/);
    assert.deepEqual(writes(store), []);
    assert.equal(store.blobs.size, 0);
  }
});

test("a gallery picture deleted after approval blocks the publish before any upload", async () => {
  const { shop, store } = harness({ docs: [{ id: "g-1", type: "image", title: "Sakura", blobName: "projects/portfolio/g-1.jpg" }] });
  const code = codeFrom(
    await shop.tools.shop_add_item({ title: "Bowl", price_jpy: "3000", image_paths: [photo("b.jpg")], gallery_image_ids: ["g-1"] }),
  );
  store.items.delete("image/g-1");
  assert.match(text(await shop.tools.shop_confirm({ approval_code: code })), /No gallery image found/);
  assert.deepEqual(writes(store), []);
});

test("if saving the item fails, uploaded photos are removed and a retry does not duplicate", async () => {
  const { shop, store } = harness();
  const code = codeFrom(await shop.tools.shop_add_item({ title: "Bowl", price_jpy: "3000", image_paths: [photo("b.jpg")] }));
  store.fail.create = codedError(503, "Service\n  unavailable");
  assert.equal(text(await shop.tools.shop_confirm({ approval_code: code })), "Couldn't save the shop change: Service unavailable");
  assert.equal(store.blobs.size, 0);
  assert.equal(store.products().length, 0);

  store.fail.create = null;
  assert.match(text(await shop.tools.shop_confirm({ approval_code: code })), /Added "Bowl"/);
  assert.equal(store.products().length, 1);
  assert.equal(store.blobs.size, 1);
  assert.equal(store.products()[0].images[0].blobName, [...store.blobs.keys()][0]);
});

test("a retry after the item was already saved reports it instead of creating a second one", async () => {
  const { shop, store } = harness();
  const code = codeFrom(await shop.tools.shop_add_item({ title: "Bowl", price_jpy: "3000", image_paths: [photo("b.jpg")] }));
  const realCreate = store.container.items.create;
  // First attempt: the write lands but the response is lost.
  store.container.items.create = async (doc) => {
    await realCreate(doc);
    throw codedError(408, "request timed out");
  };
  assert.match(text(await shop.tools.shop_confirm({ approval_code: code })), /request timed out/);
  store.container.items.create = realCreate;
  assert.match(text(await shop.tools.shop_confirm({ approval_code: code })), /Added "Bowl" to the shop: ¥3,000/);
  assert.equal(store.products().length, 1);
});

// ---------- changing items ----------

test("shop_update_item changes only what was asked and keeps the page address", async () => {
  const { shop, store, clock } = harness({ docs: [product()] });
  const staged = await shop.tools.shop_update_item({
    item: "/shop/item/cherry-blossom-print",
    title: "Sakura Print",
    price_jpy: "¥9,500",
    description: "New text",
  });
  const summary = text(staged);
  assert.ok(summary.includes('Name: "Cherry Blossom Print" → "Sakura Print" (the page address stays /shop/item/cherry-blossom-print)'));
  assert.ok(summary.includes("Price: ¥8,000 → ¥9,500"));
  clock.value = new Date("2026-10-06T04:00:00.000Z");
  assert.equal(
    text(await shop.tools.shop_confirm({ approval_code: codeFrom(staged) })),
    'Updated "Sakura Print": name → "Sakura Print", price → ¥9,500, description updated. Page: /shop/item/cherry-blossom-print.',
  );
  const doc = store.items.get("product/p-1");
  assert.deepEqual(
    { title: doc.title, slug: doc.slug, priceJpy: doc.priceJpy, description: doc.description, stock: doc.stock, status: doc.status },
    { title: "Sakura Print", slug: "cherry-blossom-print", priceJpy: 9500, description: "New text", stock: 3, status: "active" },
  );
  assert.equal(doc.updatedAt, "2026-10-06T04:00:00.000Z");
  assert.equal(doc.createdAt, "2026-09-01T00:00:00.000Z");
  assert.deepEqual(doc.images, product().images);
  // Guarded against a concurrent sale lowering stock.
  assert.deepEqual(store.log.find(([kind]) => kind === "replace")[2], {
    accessCondition: { type: "IfMatch", condition: "etag-1" },
  });
});

test("stock changes move an item between on-sale and sold out, but never un-hide it", async () => {
  const cases = [
    [{ stock: 3, status: "active" }, "0", 0, "sold"],
    [{ stock: 0, status: "sold" }, "2", 2, "active"],
    [{ stock: 0, status: "sold" }, "unlimited", null, "sold"],
    [{ stock: 3, status: "hidden" }, "0", 0, "hidden"],
    [{ stock: 0, status: "hidden" }, "4", 4, "hidden"],
    [{ stock: null, status: "active" }, "1", 1, "active"],
  ];
  for (const [start, stock, expectedStock, expectedStatus] of cases) {
    const { shop, store } = harness({ docs: [product(start)] });
    const staged = await shop.tools.shop_update_item({ item: "p-1", stock });
    await shop.tools.shop_confirm({ approval_code: codeFrom(staged) });
    const doc = store.items.get("product/p-1");
    assert.equal(doc.stock, expectedStock, JSON.stringify(start));
    assert.equal(doc.status, expectedStatus, JSON.stringify(start));
  }
});

test("shop_update_item refuses bad input, unknown items and empty changes without staging", async () => {
  const cases = [
    [{ item: "p-1" }, /Nothing to change/],
    [{ item: "p-1", price_jpy: "free" }, /not a valid price in yen/],
    [{ item: "p-1", stock: "-2" }, /Stock must be/],
    [{ item: "p-1", title: " " }, /can't be empty/],
    [{ item: "no-such-item", price_jpy: "100" }, /No shop item matches "no-such-item"/],
    [{ price_jpy: "100" }, /Say which item/],
  ];
  for (const [params, expected] of cases) {
    const { shop, store, stateDir } = harness({ docs: [product()] });
    assert.match(text(await shop.tools.shop_update_item(params)), expected);
    assert.deepEqual(writes(store), []);
    assert.ok(!existsSync(join(stateDir, "shop-pending.json")));
  }
});

test("a sale landing between approval and confirm is not overwritten", async () => {
  const { shop, store } = harness({ docs: [product()] });
  const code = codeFrom(await shop.tools.shop_update_item({ item: "p-1", price_jpy: "9000" }));
  store.fail.replace = codedError(412);
  assert.match(text(await shop.tools.shop_confirm({ approval_code: code })), /changed on the site at the same moment/);
  assert.equal(store.items.get("product/p-1").priceJpy, 8000);
  store.fail.replace = null;
  assert.match(text(await shop.tools.shop_confirm({ approval_code: code })), /price → ¥9,000/);
});

test("an item deleted after approval is reported, not recreated", async () => {
  const { shop, store } = harness({ docs: [product()] });
  const code = codeFrom(await shop.tools.shop_mark_sold({ item: "p-1" }));
  store.items.delete("product/p-1");
  assert.match(text(await shop.tools.shop_confirm({ approval_code: code })), /no longer in the shop/);
  assert.deepEqual(writes(store), []);
});

test("shop_mark_sold sets sold and zeroes counted stock only", async () => {
  for (const [stock, expectedStock] of [[3, 0], [1, 0], [null, null]]) {
    const { shop, store } = harness({ docs: [product({ stock })] });
    const staged = await shop.tools.shop_mark_sold({ item: "cherry-blossom-print" });
    assert.match(text(staged), /Ready to mark "Cherry Blossom Print" \(¥8,000, \/shop\/item\/cherry-blossom-print\) as sold out/);
    assert.equal(
      text(await shop.tools.shop_confirm({ approval_code: codeFrom(staged) })),
      '"Cherry Blossom Print" is now shown as sold out. Page: /shop/item/cherry-blossom-print.',
    );
    const doc = store.items.get("product/p-1");
    assert.equal(doc.status, "sold");
    assert.equal(doc.stock, expectedStock);
  }
  const { shop, store } = harness({ docs: [product({ status: "sold", stock: 0 })] });
  assert.match(text(await shop.tools.shop_mark_sold({ item: "p-1" })), /already marked as sold out/);
  assert.deepEqual(writes(store), []);
});

test("shop_set_visibility hides and shows, returning sold-out items as sold out", async () => {
  const cases = [
    [{ status: "active", stock: 3 }, false, "hidden"],
    [{ status: "sold", stock: 0 }, false, "hidden"],
    [{ status: "hidden", stock: 3 }, true, "active"],
    [{ status: "hidden", stock: null }, true, "active"],
    [{ status: "hidden", stock: 0 }, true, "sold"],
  ];
  for (const [start, visible, expected] of cases) {
    const { shop, store } = harness({ docs: [product(start)] });
    const staged = await shop.tools.shop_set_visibility({ item: "p-1", visible });
    assert.equal(store.items.get("product/p-1").status, start.status);
    await shop.tools.shop_confirm({ approval_code: codeFrom(staged) });
    const doc = store.items.get("product/p-1");
    assert.equal(doc.status, expected, JSON.stringify(start));
    assert.equal(doc.stock, start.stock);
  }
  for (const [start, visible, expected] of [
    [{ status: "active" }, true, /already visible in the shop/],
    [{ status: "hidden" }, false, /already hidden from the shop/],
  ]) {
    const { shop, store } = harness({ docs: [product(start)] });
    assert.match(text(await shop.tools.shop_set_visibility({ item: "p-1", visible })), expected);
    assert.deepEqual(writes(store), []);
  }
  const { shop } = harness({ docs: [product()] });
  assert.match(text(await shop.tools.shop_set_visibility({ item: "p-1", visible: "yes" })), /visible true \(show\) or false \(hide\)/);
});

// ---------- listing ----------

test("shop_list_items shows products only — never orders or customers", async () => {
  const { shop, store } = harness({
    docs: [
      product(),
      product({ id: "p-2", title: "Original Ink", slug: "original-ink", priceJpy: 120000, stock: 1, status: "hidden" }),
      product({ id: "p-3", title: "Tote", slug: "tote", priceJpy: 2500, stock: null }),
      product({ id: "p-4", title: "Gone", slug: "gone", stock: 0, status: "sold" }),
      { id: "o-1", type: "order", customerName: "Private Person", customerEmail: "private@example.invalid", shippingAddress: { line1: "1 Secret St" } },
      { id: "store-settings", type: "settings", shippingFlatJpy: 1000 },
    ],
  });
  const listing = text(await shop.tools.shop_list_items());
  assert.equal(
    listing,
    [
      "4 item(s) in the shop:",
      "• Cherry Blossom Print — ¥8,000 — 3 in stock — visible in the shop",
      "  id: p-1 · slug: cherry-blossom-print · page: /shop/item/cherry-blossom-print",
      "• Original Ink — ¥120,000 — 1 in stock (one of a kind) — hidden from the shop",
      "  id: p-2 · slug: original-ink · page: /shop/item/original-ink",
      "• Tote — ¥2,500 — made to order (never sells out) — visible in the shop",
      "  id: p-3 · slug: tote · page: /shop/item/tote",
      "• Gone — ¥8,000 — sold out — shown as sold out",
      "  id: p-4 · slug: gone · page: /shop/item/gone",
    ].join("\n"),
  );
  assert.doesNotMatch(listing, /Private|Secret|example\.invalid/);
  assert.deepEqual(writes(store), []);
  const [, query] = store.log.find(([kind]) => kind === "query");
  assert.doesNotMatch(query, /SELECT \*/);

  assert.equal(text(await harness().shop.tools.shop_list_items()), "The shop is empty.");
});

// ---------- configuration / failures ----------

test("every shop tool is inert without the tenant's site config, and never opens a client", async () => {
  for (const config of [{}, { ...CONFIG, blobAccount: "" }, null]) {
    const h = harness({ config });
    for (const [name, params] of [
      ["shop_list_items", {}],
      ["shop_add_item", { title: "Bowl", price_jpy: "3000", image_paths: [photo("b.jpg")] }],
      ["shop_update_item", { item: "p-1", price_jpy: "1" }],
      ["shop_mark_sold", { item: "p-1" }],
      ["shop_set_visibility", { item: "p-1", visible: false }],
      ["shop_confirm", { approval_code: "ABCDEF" }],
    ]) {
      assert.equal(text(await h.shop.tools[name](params)), "Site publishing isn't configured for this account.", name);
    }
    assert.equal(h.opened(), 0);
  }
});

test("a storage failure comes back as one short plain line", async () => {
  const { shop, store } = harness({ docs: [product()] });
  store.fail.query = new Error(`Forbidden\n\n   ${"detail ".repeat(100)}`);
  const result = text(await shop.tools.shop_list_items());
  assert.match(result, /^Couldn't list the shop items: Forbidden detail/);
  assert.ok(result.length < 340);
  assert.doesNotMatch(result, /\n/);
});

// ---------- plugin registration ----------

test("index.js registers the shop tools the manifest declares, without touching Azure", async () => {
  const register = require("./index.js");
  const manifest = JSON.parse(readFileSync(new URL("./openclaw.plugin.json", import.meta.url), "utf8"));
  const tools = new Map();
  register({ pluginConfig: {}, registerTool: (tool) => tools.set(tool.name, tool) });
  assert.deepEqual([...tools.keys()], manifest.contracts.tools);
  assert.deepEqual([...tools.keys()], [
    "publish_portfolio_image",
    "shop_list_items",
    "shop_add_item",
    "shop_update_item",
    "shop_mark_sold",
    "shop_set_visibility",
    "shop_confirm",
  ]);
  // Only shop_confirm writes, and its one input is the approval code.
  assert.deepEqual(tools.get("shop_confirm").parameters.required, ["approval_code"]);
  assert.deepEqual(Object.keys(tools.get("shop_confirm").parameters.properties), ["approval_code"]);
  for (const name of ["shop_add_item", "shop_update_item", "shop_mark_sold", "shop_set_visibility"]) {
    assert.match(tools.get(name).description, /does NOT change the site/, name);
    assert.match(tools.get(name).description, /shop_confirm/, name);
  }
  // Both OpenClaw calling conventions reach the tool with the params object.
  const unconfigured = "Site publishing isn't configured for this account.";
  const canonical = await tools.get("shop_confirm").execute("call-1", { approval_code: "ABCDEF" });
  const legacy = await tools.get("shop_mark_sold").execute({ item: "p-1" });
  assert.equal(text(canonical), unconfigured);
  assert.equal(text(legacy), unconfigured);
});
