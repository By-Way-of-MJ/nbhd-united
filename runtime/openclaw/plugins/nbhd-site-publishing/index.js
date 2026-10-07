/**
 * NBHD Site Publishing Plugin
 *
 * Lets a subscriber's assistant publish a portfolio image to the subscriber's
 * own website by writing directly to their Azure Blob Storage + Cosmos DB,
 * authenticating as the tenant's user-assigned managed identity (no stored
 * keys). The shop_* tools (shop.js) manage the items for sale in the same
 * Cosmos container, behind an approval code.
 *
 * Per-tenant and inert by default: the tool no-ops unless the tenant's
 * `site_config` (injected via api.pluginConfig by config_generator when
 * `site_publishing_enabled` is set) supplies the target Cosmos/Blob
 * coordinates. config_generator only loads this plugin for flagged tenants,
 * so it never even registers for anyone else.
 *
 * Auth: DefaultAzureCredential bound to the container's AZURE_CLIENT_ID
 * (the user-assigned identity mi-nbhd-<tenant>). Requires data-plane RBAC on
 * the target resources:
 *   - Storage Blob Data Contributor on the storage account
 *   - Cosmos DB Built-in Data Contributor on the Cosmos account
 * Both are additive to (and independent of) any key-based access the site
 * already uses.
 */

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const { wrapTool } = require("../../tool-logger.js");
const { createShop } = require("./shop.js");
const wrap = (def) => wrapTool(def, { plugin: "nbhd-site-publishing" });

// OpenClaw calls execute(toolCallId, params); a few legacy callers pass the
// params object first. Accept both, like tool-logger's required-arg guard.
function toolParams(args) {
  const isPlainObject = (v) => v != null && typeof v === "object" && !Array.isArray(v);
  if (isPlainObject(args[1])) return args[1];
  if (isPlainObject(args[0])) return args[0];
  return {};
}

const ITEM_PARAM = {
  type: "string",
  description: "The item's id or page name (slug) exactly as returned by shop_list_items.",
};
const APPROVAL_NOTE =
  "This does NOT change the site: it checks the request and returns a summary plus an approval code. " +
  "Show the user that summary, wait for their explicit yes, then call shop_confirm with the code.";

// Lazy-require the Azure SDKs so a missing dependency degrades to a clear
// tool-level error instead of crashing plugin load for the whole agent.
function loadAzure() {
  const { DefaultAzureCredential } = require("@azure/identity");
  const { BlobServiceClient } = require("@azure/storage-blob");
  const { CosmosClient } = require("@azure/cosmos");
  return { DefaultAzureCredential, BlobServiceClient, CosmosClient };
}

const CONTENT_TYPES = {
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".webp": "image/webp",
  ".gif": "image/gif",
};

module.exports = function register(api) {
  const cfg = (api.pluginConfig && typeof api.pluginConfig === "object") ? api.pluginConfig : {};

  api.registerTool(wrap({
    name: "publish_portfolio_image",
    description:
      "Publish ONE image to the subscriber's own portfolio website. Call this whenever the user " +
      "sends one or more images and asks to add, publish, or update them on their site, portfolio, " +
      "website, or gallery — exactly once per image (N images = N calls), passing `image_path` and " +
      "a `title`. If no title is supplied, generate a title from the image or ask once; reuse the shared theme across all images. " +
      "Never tell the user an image is live, added, or published unless this call " +
      "returned success this turn; do not claim a publish you did not actually make.",
    parameters: {
      type: "object",
      required: ["image_path", "title"],
      properties: {
        image_path: {
          type: "string",
          description: "Absolute path to the image file the user provided (e.g. the photo they just sent).",
        },
        title: {
          type: "string",
          description: "Short display title for the portfolio piece.",
        },
        description: {
          type: "string",
          description: "Optional caption / description shown with the image.",
        },
        tags: {
          type: "array",
          items: { type: "string" },
          description: "Optional list of tags.",
        },
        featured: {
          type: "boolean",
          description: "Optional. Set true to feature this image on the homepage.",
        },
      },
    },
    async execute({ image_path, title, description, tags, featured }) {
      // Self-gate: inert unless this tenant has site_config injected.
      if (!cfg.cosmosEndpoint || !cfg.cosmosDatabase || !cfg.cosmosContainer || !cfg.blobAccount || !cfg.blobContainer) {
        return { content: [{ type: "text", text: "Site publishing isn't configured for this account." }] };
      }
      if (!image_path || !fs.existsSync(image_path)) {
        return { content: [{ type: "text", text: `Error: no image file found at ${image_path}.` }] };
      }
      if (!title || !title.trim()) {
        return { content: [{ type: "text", text: "Error: a title is required." }] };
      }

      let Azure;
      try {
        Azure = loadAzure();
      } catch {
        return { content: [{ type: "text", text: "Site publishing is temporarily unavailable (dependencies missing)." }] };
      }
      const { DefaultAzureCredential, BlobServiceClient, CosmosClient } = Azure;

      try {
        const buffer = fs.readFileSync(image_path);
        const ext = (path.extname(image_path) || ".jpg").toLowerCase();
        const contentType = CONTENT_TYPES[ext] || "application/octet-stream";
        const id = crypto.randomUUID();
        const fileName = path.basename(image_path);
        const prefix = String(cfg.blobPathPrefix || "projects/portfolio").replace(/\/+$/, "");
        const blobName = `${prefix}/${id}${ext}`;
        const now = new Date().toISOString();

        // Authenticate as the tenant's user-assigned managed identity.
        const credential = new DefaultAzureCredential({
          managedIdentityClientId: process.env.AZURE_CLIENT_ID,
        });

        // 1. Upload the image to Blob Storage.
        const blobService = new BlobServiceClient(
          `https://${cfg.blobAccount}.blob.core.windows.net`,
          credential,
        );
        await blobService
          .getContainerClient(cfg.blobContainer)
          .getBlockBlobClient(blobName)
          .uploadData(buffer, { blobHTTPHeaders: { blobContentType: contentType } });

        // 2. Write the portfolio metadata doc the site reads. Schema mirrors
        //    api/shared/models.js PortfolioImage — crucially type:'image' and
        //    isActive:true, which the site's getImages()/getFeaturedImages()
        //    filter on. (The site's own uploaders omit these, so their images
        //    only surface on category pages — this writes complete docs.)
        const cosmos = new CosmosClient({ endpoint: cfg.cosmosEndpoint, aadCredentials: credential });
        const doc = {
          id,
          type: "image", // partition key (/type) + the site's primary filter
          title: title.trim(),
          description: (description || "").trim(),
          categoryId: null,
          blobName,
          thumbnailBlobName: blobName, // no separate thumbnail (matches current site behavior)
          fileName,
          contentType,
          size: buffer.length,
          width: 0, // dimensions not computed client-side; site tolerates 0
          height: 0,
          order: 0,
          tags: Array.isArray(tags) ? tags.filter((t) => typeof t === "string") : [],
          isActive: true,
          isFeatured: Boolean(featured),
          createdAt: now,
          updatedAt: now,
        };
        await cosmos.database(cfg.cosmosDatabase).container(cfg.cosmosContainer).items.create(doc);

        return {
          content: [{
            type: "text",
            text: `Published "${doc.title}" to the portfolio (id ${id}). It should appear on the site within a minute.`,
          }],
        };
      } catch (err) {
        const msg = (err && err.message ? err.message : String(err)).replace(/\s+/g, " ").slice(0, 300);
        return { content: [{ type: "text", text: `Couldn't publish the image: ${msg}` }] };
      }
    },
  }));

  // ---------- shop (items for sale) ----------

  const shop = createShop({
    config: cfg,
    env: process.env,
    openStore() {
      let Azure;
      try {
        Azure = loadAzure();
      } catch {
        throw new Error("Site publishing is temporarily unavailable (dependencies missing).");
      }
      const { DefaultAzureCredential, BlobServiceClient, CosmosClient } = Azure;
      // Same identity and coordinates as publish_portfolio_image above.
      const credential = new DefaultAzureCredential({
        managedIdentityClientId: process.env.AZURE_CLIENT_ID,
      });
      return {
        container: new CosmosClient({ endpoint: cfg.cosmosEndpoint, aadCredentials: credential })
          .database(cfg.cosmosDatabase)
          .container(cfg.cosmosContainer),
        blobContainer: new BlobServiceClient(`https://${cfg.blobAccount}.blob.core.windows.net`, credential)
          .getContainerClient(cfg.blobContainer),
      };
    },
  });

  api.registerTool(wrap({
    name: "shop_list_items",
    description:
      "List every item for sale in the user's online shop: name, price, stock, whether it is visible, " +
      "hidden or sold out, and its id and page name (slug). Call this to find the item before changing it.",
    parameters: { type: "object", additionalProperties: false, properties: {} },
    async execute() {
      return shop.tools.shop_list_items();
    },
  }));

  api.registerTool(wrap({
    name: "shop_add_item",
    description:
      "Prepare a new item for sale in the user's online shop (a product with a price — not a gallery " +
      "picture; gallery pictures use publish_portfolio_image). Needs a name, a price in yen the user " +
      "actually stated (never guess or invent a price) and at least one photo. " + APPROVAL_NOTE,
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["title", "price_jpy"],
      properties: {
        title: { type: "string", description: "Item name shown in the shop." },
        price_jpy: {
          type: "string",
          description: 'Price in whole Japanese yen exactly as the user gave it, e.g. "8000" or "¥8,000".',
        },
        image_paths: {
          type: "array",
          items: { type: "string" },
          description: "Absolute paths of the photo files the user sent for this item (jpg, png, gif or webp).",
        },
        gallery_image_ids: {
          type: "array",
          items: { type: "string" },
          description: "Ids of pictures already in the site gallery to use as this item's photos.",
        },
        stock: {
          type: "string",
          description: 'How many exist, e.g. "10", or "unlimited" for made-to-order. Omit for made-to-order.',
        },
        one_of_a_kind: {
          type: "boolean",
          description: "True for an original: exactly one exists and it sells out after one purchase.",
        },
        product_type: {
          type: "string",
          enum: ["art", "original", "print", "merch"],
          description: "Kind of item. Default: art.",
        },
        description: { type: "string", description: "Optional longer text shown on the item page." },
        hidden: {
          type: "boolean",
          description: "True to add the item without showing it in the shop yet. Default: false.",
        },
      },
    },
    async execute(...args) {
      return shop.tools.shop_add_item(toolParams(args));
    },
  }));

  api.registerTool(wrap({
    name: "shop_update_item",
    description:
      "Prepare a change to an existing shop item's price, stock, name or description. Pass only what " +
      "the user asked to change; never guess a price. The page address stays the same when the name changes. " +
      APPROVAL_NOTE,
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["item"],
      properties: {
        item: ITEM_PARAM,
        title: { type: "string", description: "New item name." },
        price_jpy: {
          type: "string",
          description: 'New price in whole Japanese yen exactly as the user gave it, e.g. "9000".',
        },
        stock: { type: "string", description: 'New number in stock, e.g. "3", or "unlimited" for made-to-order.' },
        product_type: { type: "string", enum: ["art", "original", "print", "merch"], description: "New kind of item." },
        description: { type: "string", description: "New text for the item page." },
      },
    },
    async execute(...args) {
      return shop.tools.shop_update_item(toolParams(args));
    },
  }));

  api.registerTool(wrap({
    name: "shop_mark_sold",
    description:
      "Prepare marking a shop item as sold out (for example it was sold in person). " + APPROVAL_NOTE,
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["item"],
      properties: { item: ITEM_PARAM },
    },
    async execute(...args) {
      return shop.tools.shop_mark_sold(toolParams(args));
    },
  }));

  api.registerTool(wrap({
    name: "shop_set_visibility",
    description:
      "Prepare hiding a shop item from the shop, or showing a hidden item again. " + APPROVAL_NOTE,
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["item", "visible"],
      properties: {
        item: ITEM_PARAM,
        visible: { type: "boolean", description: "False to hide the item, true to show it again." },
      },
    },
    async execute(...args) {
      return shop.tools.shop_set_visibility(toolParams(args));
    },
  }));

  api.registerTool(wrap({
    name: "shop_confirm",
    description:
      "Save ONE prepared shop change to the live site. Call only after the user has seen the summary " +
      "returned by shop_add_item / shop_update_item / shop_mark_sold / shop_set_visibility and explicitly " +
      "said yes, passing that summary's approval code. Never say an item is added, changed, sold or hidden " +
      "unless this call returned success this turn.",
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["approval_code"],
      properties: {
        approval_code: {
          type: "string",
          description: "Six-character code returned with the summary the user approved.",
        },
      },
    },
    async execute(...args) {
      return shop.tools.shop_confirm(toolParams(args));
    },
  }));
};
