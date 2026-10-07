"use strict";

/**
 * Shop tools for the NBHD Site Publishing plugin.
 *
 * Writes the same Cosmos `type: 'product'` documents (and `images/<uuid>.<ext>`
 * blobs) that the site's own `scripts/manage-store.js` writes, so the live
 * shop API reads them unchanged. That script is the contract: field names,
 * slug rules, status transitions and blob layout below are ports of it.
 *
 * Facts are decided here, not by the model: price and stock parsing, slug,
 * ids, timestamps, image checks and status transitions. Nothing reaches the
 * site until the user has seen a summary — every change tool only STAGES the
 * change and returns a short approval code; `shop_confirm` is the single
 * tool that writes, and it takes nothing but that code, so what is published
 * is exactly what was summarised.
 *
 * Orders and customer data are deliberately out of reach: every query here is
 * pinned to `type = 'product'` (plus single reads of `type = 'image'` gallery
 * docs when an existing picture is reused).
 */

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const NOT_CONFIGURED = "Site publishing isn't configured for this account.";
const PENDING_STATE_VERSION = 1;
const APPROVAL_TTL_MS = 24 * 60 * 60 * 1000;
const APPROVAL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const MAX_PENDING = 20;
const MAX_IMAGES = 8;
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
// Stripe's ceiling for a JPY charge; a larger price could never be checked out.
const MAX_PRICE_JPY = 99999999;
const MAX_STOCK = 100000;
const MAX_TITLE_CHARS = 120;
const MAX_DESCRIPTION_CHARS = 4000;
const MAX_SLUG_ATTEMPTS = 50;
const BLOB_CACHE_CONTROL = "public, max-age=31536000";
// Must match scripts/manage-store.js CONTENT_TYPES.
const CONTENT_TYPES = {
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".gif": "image/gif",
  ".webp": "image/webp",
};
const PRODUCT_TYPES = ["art", "original", "print", "merch"];
const PRODUCT_LIST_QUERY =
  'SELECT c.id, c.slug, c.title, c.priceJpy, c.stock, c.status FROM c WHERE c.type = @type ORDER BY c["order"] ASC';

function toolText(text) {
  return { content: [{ type: "text", text }] };
}

function workspaceRoot(env) {
  return env.OPENCLAW_WORKSPACE_PATH ||
    env.OPENCLAW_WORKSPACE ||
    path.join(env.OPENCLAW_HOME || "/home/node/.openclaw", "workspace");
}

function approvalCode() {
  return Array.from(crypto.randomBytes(6), (byte) => APPROVAL_ALPHABET[byte & 31]).join("");
}

function toAsciiDigits(text) {
  return text
    .replace(/[０-９]/g, (char) => String.fromCharCode(char.charCodeAt(0) - 0xfee0))
    .replace(/，/g, ",");
}

function formatYen(amount) {
  if (amount === null || amount === undefined) return "-";
  return `¥${String(amount).replace(/\B(?=(\d{3})+(?!\d))/g, ",")}`;
}

/**
 * Integer yen from "12000", "¥12,000" or "12,000 yen" (the forms
 * manage-store.js documents), with identical results for those. Anything
 * else — decimals, "12k", "1.2万", negatives — is refused rather than
 * digit-stripped: the script would read "8,000.00" as ¥800,000.
 */
function parseYen(value) {
  let amount;
  if (typeof value === "number") {
    amount = value;
  } else if (typeof value === "string") {
    const match = toAsciiDigits(value.trim()).match(
      /^(?:[¥￥]|jpy)?\s*(\d+|\d{1,3}(?:,\d{3})+)\s*(?:yen|円|jpy)?$/i,
    );
    amount = match ? Number(match[1].replace(/,/g, "")) : NaN;
  }
  if (!Number.isSafeInteger(amount) || amount < 1) {
    throw new Error(`"${String(value)}" is not a valid price in yen. Give a whole number of yen, like 8000 or ¥8,000.`);
  }
  if (amount > MAX_PRICE_JPY) {
    throw new Error(`The price can be at most ${formatYen(MAX_PRICE_JPY)}.`);
  }
  return amount;
}

/** Whole number >= 0, or null for "unlimited" (made to order, never sells out). */
function parseStock(value) {
  if (typeof value === "string" && value.trim().toLowerCase() === "unlimited") return null;
  let count;
  if (typeof value === "number") {
    count = value;
  } else if (typeof value === "string" && /^\d+$/.test(toAsciiDigits(value.trim()))) {
    count = Number(toAsciiDigits(value.trim()));
  }
  if (!Number.isSafeInteger(count) || count < 0 || count > MAX_STOCK) {
    throw new Error(`Stock must be a whole number from 0 to ${MAX_STOCK}, or "unlimited" — got "${String(value)}".`);
  }
  return count;
}

/** Port of manage-store.js slugify (ASCII-only; other titles get item-<8 hex>). */
function slugify(title, randomUUID = crypto.randomUUID) {
  const slug = String(title)
    .toLowerCase()
    .replace(/[^a-z0-9 -]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
  return slug || `item-${randomUUID().slice(0, 8)}`;
}

function describeStock(product) {
  if (product.status === "sold") return "sold out";
  if (product.stock === null || product.stock === undefined) return "made to order (never sells out)";
  if (product.stock === 1) return "1 in stock (one of a kind)";
  return `${product.stock} in stock`;
}

function describeStockValue(stock) {
  return describeStock({ stock, status: stock === 0 ? "sold" : "active" });
}

function describeStatus(status) {
  if (status === "hidden") return "hidden from the shop";
  if (status === "sold") return "shown as sold out";
  return "visible in the shop";
}

/** Extension the file's own bytes say it is, or null when it isn't an allowed image. */
function sniffImageExtension(buffer) {
  if (buffer.length >= 3 && buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff) return ".jpg";
  if (buffer.length >= 8 && buffer.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
    return ".png";
  }
  const head = buffer.subarray(0, 12).toString("latin1");
  if (head.startsWith("GIF87a") || head.startsWith("GIF89a")) return ".gif";
  if (head.startsWith("RIFF") && head.slice(8, 12) === "WEBP") return ".webp";
  return null;
}

function cleanText(value, label, maxChars, { multiline = false } = {}) {
  if (typeof value !== "string") throw new Error(`${label} must be text.`);
  const text = value.trim();
  if (text.length > maxChars) throw new Error(`${label} can be at most ${maxChars} characters.`);
  if (!multiline && /[\r\n]/.test(text)) throw new Error(`${label} must be a single line.`);
  return text;
}

function isProvided(value) {
  return value !== undefined && value !== null;
}

function errorCode(error) {
  return error ? error.code ?? error.statusCode : undefined;
}

function createShop({
  config,
  env = process.env,
  now = () => new Date(),
  randomUUID = crypto.randomUUID,
  openStore,
  stateDir,
} = {}) {
  const cfg = config && typeof config === "object" ? config : {};
  const configured = Boolean(
    cfg.cosmosEndpoint && cfg.cosmosDatabase && cfg.cosmosContainer && cfg.blobAccount && cfg.blobContainer,
  );
  const pendingStateDir = stateDir || path.join(workspaceRoot(env), ".nbhd-site-publishing");
  const pendingStateFile = path.join(pendingStateDir, "shop-pending.json");

  function timestamp() {
    const value = now();
    return value instanceof Date ? value : new Date(value);
  }

  // ---------- staged changes (survive a container restart) ----------

  function loadPending() {
    let stored;
    try {
      stored = JSON.parse(fs.readFileSync(pendingStateFile, "utf8"));
    } catch {
      return {};
    }
    if (!stored || stored.version !== PENDING_STATE_VERSION || !stored.pending || typeof stored.pending !== "object") {
      return {};
    }
    const current = timestamp().getTime();
    const pending = {};
    for (const [code, entry] of Object.entries(stored.pending)) {
      const issuedAt = entry && typeof entry === "object" ? Date.parse(entry.issuedAt) : NaN;
      const age = current - issuedAt;
      if (!Number.isFinite(issuedAt) || age < 0 || age > APPROVAL_TTL_MS) continue;
      if (!entry.op || typeof entry.op !== "object" || typeof entry.target !== "string") continue;
      pending[code] = entry;
    }
    return pending;
  }

  function savePending(pending) {
    if (Object.keys(pending).length === 0) {
      try {
        fs.unlinkSync(pendingStateFile);
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
      return;
    }
    fs.mkdirSync(pendingStateDir, { recursive: true });
    const tempFile = `${pendingStateFile}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`;
    fs.writeFileSync(tempFile, JSON.stringify({ version: PENDING_STATE_VERSION, pending }), { mode: 0o600 });
    fs.renameSync(tempFile, pendingStateFile);
  }

  /** Stage a change and return the text the model must show the user. */
  function stage(target, op, summaryLines) {
    const pending = loadPending();
    // A newer change to the same thing replaces the older one, so a corrected
    // price can never be confirmed with the stale code.
    for (const [code, entry] of Object.entries(pending)) {
      if (entry.target === target) delete pending[code];
    }
    const codes = Object.keys(pending).sort((a, b) => Date.parse(pending[a].issuedAt) - Date.parse(pending[b].issuedAt));
    while (codes.length >= MAX_PENDING) delete pending[codes.shift()];

    let code = approvalCode();
    while (pending[code]) code = approvalCode();
    pending[code] = { target, op, issuedAt: timestamp().toISOString() };
    savePending(pending);
    return toolText(
      `${summaryLines.join("\n")}\n\n` +
        `Nothing has changed on the site yet. Approval code: ${code}. Show the user this summary and wait for ` +
        "their explicit yes, then call shop_confirm with this code.",
    );
  }

  // ---------- site data ----------

  let store = null;
  async function getStore() {
    if (!store) {
      if (typeof openStore !== "function") throw new Error("Site publishing is temporarily unavailable.");
      store = await openStore();
    }
    return store;
  }

  async function findProduct(container, idOrSlug) {
    if (typeof idOrSlug !== "string" || !idOrSlug.trim()) {
      throw new Error("Say which item: its id or page name (slug) from shop_list_items.");
    }
    const value = idOrSlug.trim().replace(/^\/?shop\/item\//, "");
    const { resources } = await container.items
      .query({
        query: "SELECT * FROM c WHERE c.type = @type AND (c.id = @v OR c.slug = @v)",
        parameters: [
          { name: "@type", value: "product" },
          { name: "@v", value },
        ],
      })
      .fetchAll();
    if (resources.length === 0) {
      throw new Error(`No shop item matches "${value}". Call shop_list_items to see every item.`);
    }
    return resources[0];
  }

  async function readProduct(container, id) {
    let resource;
    try {
      ({ resource } = await container.item(id, "product").read());
    } catch (error) {
      if (errorCode(error) !== 404) throw error;
    }
    if (!resource) throw new Error("That item is no longer in the shop. Call shop_list_items and start again.");
    return resource;
  }

  async function replaceProduct(container, product) {
    product.updatedAt = timestamp().toISOString();
    try {
      // The site's Stripe webhook lowers stock on a sale; never overwrite a
      // copy that changed since it was read.
      await container.item(product.id, "product").replace(
        product,
        product._etag ? { accessCondition: { type: "IfMatch", condition: product._etag } } : undefined,
      );
    } catch (error) {
      if (errorCode(error) === 412) {
        throw new Error("The item changed on the site at the same moment (for example a sale). Nothing was saved — try again.");
      }
      throw error;
    }
  }

  async function slugExists(container, slug) {
    const { resources } = await container.items
      .query({
        query: "SELECT c.id FROM c WHERE c.type = @type AND c.slug = @slug",
        parameters: [
          { name: "@type", value: "product" },
          { name: "@slug", value: slug },
        ],
      })
      .fetchAll();
    return resources.length > 0;
  }

  async function findPortfolioImage(container, imageId) {
    let resource;
    try {
      ({ resource } = await container.item(imageId, "image").read());
    } catch (error) {
      if (errorCode(error) !== 404) throw error;
    }
    if (!resource || !resource.blobName) throw new Error(`No gallery image found with id "${imageId}".`);
    return resource;
  }

  /** Read and check one photo without touching the network. */
  function inspectImage(imagePath) {
    if (typeof imagePath !== "string" || !imagePath.trim()) throw new Error("Each photo needs a file path.");
    const resolved = imagePath.trim();
    const name = path.basename(resolved);
    let stat;
    try {
      stat = fs.statSync(resolved);
    } catch {
      throw new Error(`Photo not found: ${name}. Ask the user to send it again.`);
    }
    if (!stat.isFile()) throw new Error(`Photo not found: ${name}. Ask the user to send it again.`);
    const ext = path.extname(resolved).toLowerCase();
    if (!CONTENT_TYPES[ext]) {
      throw new Error(`Unsupported photo type "${ext || name}". Use jpg, png, gif or webp.`);
    }
    if (stat.size === 0) throw new Error(`The photo ${name} is empty.`);
    if (stat.size > MAX_IMAGE_BYTES) {
      throw new Error(`The photo ${name} is too large (limit ${MAX_IMAGE_BYTES / (1024 * 1024)} MB).`);
    }
    const buffer = fs.readFileSync(resolved);
    const actual = sniffImageExtension(buffer);
    if (!actual || CONTENT_TYPES[actual] !== CONTENT_TYPES[ext]) {
      throw new Error(`The file ${name} is not a valid ${ext.slice(1)} image.`);
    }
    return {
      path: resolved,
      name,
      ext,
      size: buffer.length,
      sha256: crypto.createHash("sha256").update(buffer).digest("hex"),
      buffer,
    };
  }

  function stringList(value, label) {
    if (!isProvided(value)) return [];
    if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || !item.trim())) {
      throw new Error(`${label} must be a list of text values.`);
    }
    return value.map((item) => item.trim());
  }

  // ---------- applying a confirmed change ----------

  async function applyAdd(op) {
    // Everything that can fail without a write happens before the first
    // upload, so a refused item never leaves orphaned photos in storage.
    const uploads = op.uploads.map((staged) => {
      const image = inspectImage(staged.path);
      if (image.sha256 !== staged.sha256) {
        throw new Error(`The photo ${image.name} changed after it was approved. Stage the item again.`);
      }
      return { ...staged, buffer: image.buffer };
    });
    const { container, blobContainer } = await getStore();

    const galleryImages = [];
    for (const imageId of op.galleryImageIds) {
      const image = await findPortfolioImage(container, imageId);
      galleryImages.push({
        imageId: image.id,
        blobName: image.blobName,
        thumbnailBlobName: image.thumbnailBlobName || image.blobName,
      });
    }

    let slug = op.slugBase;
    let suffix = 2;
    while (await slugExists(container, slug)) {
      if (suffix > MAX_SLUG_ATTEMPTS) throw new Error("Too many items share that name. Pick a different name.");
      slug = `${op.slugBase}-${suffix++}`;
    }

    const stamp = timestamp().toISOString();
    const product = {
      id: op.id,
      type: "product",
      title: op.title,
      slug,
      description: op.description,
      priceJpy: op.priceJpy,
      productType: op.productType,
      stock: op.stock,
      images: [
        // No image library in this runtime, so the full photo doubles as its
        // own thumbnail — the same fallback manage-store.js takes.
        ...uploads.map((upload) => ({ blobName: upload.blobName, thumbnailBlobName: upload.blobName })),
        ...galleryImages,
      ],
      status: op.hidden ? "hidden" : op.stock === 0 ? "sold" : "active",
      order: 0,
      createdAt: stamp,
      updatedAt: stamp,
    };

    const uploaded = [];
    try {
      for (const upload of uploads) {
        await blobContainer.getBlockBlobClient(upload.blobName).uploadData(upload.buffer, {
          blobHTTPHeaders: { blobContentType: CONTENT_TYPES[upload.ext], blobCacheControl: BLOB_CACHE_CONTROL },
        });
        uploaded.push(upload.blobName);
      }
      await container.items.create(product);
    } catch (error) {
      if (errorCode(error) === 409) {
        // The id was fixed when the item was staged, so a conflict means an
        // earlier attempt already saved this exact item.
        return describeAdded(await readProduct(container, op.id));
      }
      for (const blobName of uploaded) {
        try {
          await blobContainer.getBlockBlobClient(blobName).deleteIfExists();
        } catch {
          // Best effort: a retry reuses the same blob names and overwrites.
        }
      }
      throw error;
    }
    return describeAdded(product);
  }

  function describeAdded(product) {
    const visibility = product.status === "hidden"
      ? " It is hidden — use shop_set_visibility to show it."
      : " It should appear on the site within a minute.";
    return `Added "${product.title}" to the shop: ${formatYen(product.priceJpy)}, ${describeStock(product)}. ` +
      `Page: /shop/item/${product.slug}.${visibility}`;
  }

  async function applyUpdate(op) {
    const { container } = await getStore();
    const product = await readProduct(container, op.productId);
    const changes = [];
    const { changes: staged } = op;
    if (isProvided(staged.title)) {
      product.title = staged.title;
      changes.push(`name → "${staged.title}"`);
    }
    if (isProvided(staged.priceJpy)) {
      product.priceJpy = staged.priceJpy;
      changes.push(`price → ${formatYen(staged.priceJpy)}`);
    }
    if (isProvided(staged.description)) {
      product.description = staged.description;
      changes.push("description updated");
    }
    if (isProvided(staged.productType)) {
      product.productType = staged.productType;
      changes.push(`type → ${staged.productType}`);
    }
    if (staged.stockSet) {
      product.stock = staged.stock;
      changes.push(staged.stock === null ? "stock → made to order (never sells out)" : `stock → ${staged.stock}`);
      if (product.stock !== null && product.stock > 0 && product.status === "sold") {
        product.status = "active";
        changes.push("back on sale");
      }
      // Only flip active items to sold — hidden items stay hidden.
      if (product.stock === 0 && product.status === "active") {
        product.status = "sold";
        changes.push("marked as sold out");
      }
    }
    await replaceProduct(container, product);
    return `Updated "${product.title}": ${changes.join(", ")}. Page: /shop/item/${product.slug}.`;
  }

  async function applyStatus(op) {
    const { container } = await getStore();
    const product = await readProduct(container, op.productId);
    if (op.status === "sold") {
      product.status = "sold";
      if (product.stock !== null && product.stock !== undefined) product.stock = 0;
    } else if (op.status === "hidden") {
      product.status = "hidden";
    } else {
      product.status = visibleStatus(product);
    }
    await replaceProduct(container, product);
    return `"${product.title}" is now ${describeStatus(product.status)}. Page: /shop/item/${product.slug}.`;
  }

  // manage-store.js un-hides straight to 'active'; an item with no stock left
  // goes back as sold out instead, matching the stock-0 rule used on add.
  function visibleStatus(product) {
    return product.stock === 0 ? "sold" : "active";
  }

  async function guarded(action, failurePrefix) {
    if (!configured) return toolText(NOT_CONFIGURED);
    try {
      return await action();
    } catch (error) {
      const message = (error && error.message ? error.message : String(error)).replace(/\s+/g, " ").slice(0, 300);
      return toolText(`${failurePrefix}: ${message}`);
    }
  }

  const tools = {
    async shop_list_items() {
      return guarded(async () => {
        const { container } = await getStore();
        const { resources } = await container.items
          .query({ query: PRODUCT_LIST_QUERY, parameters: [{ name: "@type", value: "product" }] })
          .fetchAll();
        if (resources.length === 0) return toolText("The shop is empty.");
        const lines = resources.map(
          (product) =>
            `• ${product.title} — ${formatYen(product.priceJpy)} — ${describeStock(product)} — ` +
            `${describeStatus(product.status)}\n  id: ${product.id} · slug: ${product.slug} · page: /shop/item/${product.slug}`,
        );
        return toolText(`${resources.length} item(s) in the shop:\n${lines.join("\n")}`);
      }, "Couldn't list the shop items");
    },

    async shop_add_item(params = {}) {
      return guarded(async () => {
        if (!isProvided(params.title)) throw new Error("The item needs a name.");
        const title = cleanText(params.title, "The item name", MAX_TITLE_CHARS);
        if (!title) throw new Error("The item needs a name.");
        if (!isProvided(params.price_jpy) || params.price_jpy === "") {
          throw new Error("The item needs a price in yen. Ask the user — never guess a price.");
        }
        const priceJpy = parseYen(params.price_jpy);
        const description = isProvided(params.description)
          ? cleanText(params.description, "The description", MAX_DESCRIPTION_CHARS, { multiline: true })
          : "";
        const productType = isProvided(params.product_type) ? String(params.product_type).trim().toLowerCase() : "art";
        if (!PRODUCT_TYPES.includes(productType)) {
          throw new Error(`The item type must be one of: ${PRODUCT_TYPES.join(", ")}.`);
        }
        const oneOfAKind = params.one_of_a_kind === true;
        let stock = isProvided(params.stock) && params.stock !== "" ? parseStock(params.stock) : null;
        if (oneOfAKind) {
          if (isProvided(params.stock) && params.stock !== "" && stock !== 1) {
            throw new Error("A one-of-a-kind item has exactly 1 in stock. Give either one_of_a_kind or a stock number.");
          }
          stock = 1;
        }

        const imagePaths = stringList(params.image_paths, "image_paths");
        const galleryImageIds = stringList(params.gallery_image_ids, "gallery_image_ids");
        if (imagePaths.length + galleryImageIds.length === 0) {
          throw new Error("Every item needs at least one photo: one the user sent, or a gallery image id.");
        }
        if (imagePaths.length + galleryImageIds.length > MAX_IMAGES) {
          throw new Error(`An item can have at most ${MAX_IMAGES} photos.`);
        }
        if (new Set(imagePaths).size !== imagePaths.length || new Set(galleryImageIds).size !== galleryImageIds.length) {
          throw new Error("The same photo is listed twice.");
        }
        const images = imagePaths.map(inspectImage);

        const { container } = await getStore();
        const galleryTitles = [];
        for (const imageId of galleryImageIds) {
          galleryTitles.push((await findPortfolioImage(container, imageId)).title || "Untitled");
        }
        const slugBase = slugify(title, randomUUID);
        const hidden = params.hidden === true;

        const photoParts = [];
        if (images.length) photoParts.push(`${images.length} new (${images.map((image) => image.name).join(", ")})`);
        if (galleryTitles.length) {
          photoParts.push(`${galleryTitles.length} from the gallery (${galleryTitles.map((t) => `"${t}"`).join(", ")})`);
        }
        return stage(
          `add:${title.toLowerCase()}`,
          {
            kind: "add",
            id: randomUUID(),
            title,
            description,
            priceJpy,
            productType,
            stock,
            hidden,
            slugBase,
            uploads: images.map((image) => ({
              path: image.path,
              ext: image.ext,
              sha256: image.sha256,
              blobName: `images/${randomUUID()}${image.ext}`,
            })),
            galleryImageIds,
          },
          [
            "Ready to add this item to the shop:",
            `• Name: ${title}`,
            `• Price: ${formatYen(priceJpy)}`,
            `• Stock: ${describeStockValue(stock)}`,
            `• Type: ${productType}`,
            `• Photos: ${photoParts.join(" + ")}`,
            `• Description: ${description || "(none)"}`,
            `• Visibility: ${hidden ? "hidden until shown" : stock === 0 ? "shown as sold out" : "visible in the shop right away"}`,
            `• Page: /shop/item/${slugBase} (a number is added if that name is taken)`,
          ],
        );
      }, "Couldn't prepare the shop item");
    },

    async shop_update_item(params = {}) {
      return guarded(async () => {
        const { container } = await getStore();
        const product = await findProduct(container, params.item);
        const changes = {};
        const lines = [];
        if (isProvided(params.title)) {
          changes.title = cleanText(params.title, "The item name", MAX_TITLE_CHARS);
          if (!changes.title) throw new Error("The item name can't be empty.");
          lines.push(`• Name: "${product.title}" → "${changes.title}" (the page address stays /shop/item/${product.slug})`);
        }
        if (isProvided(params.price_jpy) && params.price_jpy !== "") {
          changes.priceJpy = parseYen(params.price_jpy);
          lines.push(`• Price: ${formatYen(product.priceJpy)} → ${formatYen(changes.priceJpy)}`);
        }
        if (isProvided(params.description)) {
          changes.description = cleanText(params.description, "The description", MAX_DESCRIPTION_CHARS, {
            multiline: true,
          });
          lines.push(`• Description: ${changes.description || "(removed)"}`);
        }
        if (isProvided(params.product_type)) {
          changes.productType = String(params.product_type).trim().toLowerCase();
          if (!PRODUCT_TYPES.includes(changes.productType)) {
            throw new Error(`The item type must be one of: ${PRODUCT_TYPES.join(", ")}.`);
          }
          lines.push(`• Type: ${product.productType} → ${changes.productType}`);
        }
        if (isProvided(params.stock) && params.stock !== "") {
          changes.stock = parseStock(params.stock);
          changes.stockSet = true;
          let effect = "";
          if (changes.stock === 0 && product.status === "active") effect = " — it will show as sold out";
          else if (changes.stock !== null && changes.stock > 0 && product.status === "sold") effect = " — back on sale";
          lines.push(`• Stock: ${describeStock(product)} → ${describeStockValue(changes.stock)}${effect}`);
        }
        if (lines.length === 0) {
          throw new Error("Nothing to change. Give a new name, price, stock, type or description.");
        }
        return stage(
          `item:${product.id}`,
          { kind: "update", productId: product.id, changes },
          [`Ready to change "${product.title}" (/shop/item/${product.slug}):`, ...lines],
        );
      }, "Couldn't prepare the change");
    },

    async shop_mark_sold(params = {}) {
      return guarded(async () => {
        const { container } = await getStore();
        const product = await findProduct(container, params.item);
        if (product.status === "sold") return toolText(`"${product.title}" is already marked as sold out.`);
        return stage(
          `item:${product.id}`,
          { kind: "status", productId: product.id, status: "sold" },
          [
            `Ready to mark "${product.title}" (${formatYen(product.priceJpy)}, /shop/item/${product.slug}) as sold out.`,
            "• It stays on the shop page with a sold-out label and can no longer be bought.",
          ],
        );
      }, "Couldn't prepare the change");
    },

    async shop_set_visibility(params = {}) {
      return guarded(async () => {
        if (typeof params.visible !== "boolean") {
          throw new Error("Say whether the item should be visible: visible true (show) or false (hide).");
        }
        const { container } = await getStore();
        const product = await findProduct(container, params.item);
        const isHidden = product.status === "hidden";
        if (params.visible !== isHidden) {
          return toolText(`"${product.title}" is already ${describeStatus(product.status)}.`);
        }
        const label = `"${product.title}" (${formatYen(product.priceJpy)}, /shop/item/${product.slug})`;
        return stage(
          `item:${product.id}`,
          { kind: "status", productId: product.id, status: params.visible ? "visible" : "hidden" },
          params.visible
            ? [
                `Ready to show ${label} in the shop.`,
                `• It will be ${describeStatus(visibleStatus(product))}.`,
              ]
            : [`Ready to hide ${label} from the shop.`, "• Visitors will no longer see it or its page."],
        );
      }, "Couldn't prepare the change");
    },

    async shop_confirm(params = {}) {
      return guarded(async () => {
        if (typeof params.approval_code !== "string" || !params.approval_code.trim()) {
          throw new Error("An approval code from a shop_* change tool is required.");
        }
        const code = params.approval_code.replace(/\s+/g, "").toUpperCase();
        const entry = loadPending()[code];
        if (!entry) {
          throw new Error("That approval code doesn't match a waiting change (it may have expired or been replaced). Stage the change again.");
        }
        const { op } = entry;
        let result;
        if (op.kind === "add") result = await applyAdd(op);
        else if (op.kind === "update") result = await applyUpdate(op);
        else if (op.kind === "status") result = await applyStatus(op);
        else throw new Error("That waiting change can't be read. Stage the change again.");

        const pending = loadPending();
        delete pending[code];
        savePending(pending);
        return toolText(result);
      }, "Couldn't save the shop change");
    },
  };

  return { tools, _internals: { pendingStateFile, loadPending } };
}

module.exports = {
  createShop,
  parseYen,
  parseStock,
  slugify,
  formatYen,
  sniffImageExtension,
  APPROVAL_TTL_MS,
  MAX_IMAGES,
  NOT_CONFIGURED,
};
