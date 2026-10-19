// Sync approved artwork; never derive icons from a lockup or resize exports.
// Usage: npm run generate-icons -- /path/to/nbhd-ios/Brand
const fs = require("node:fs");
const path = require("node:path");

const brand = process.argv[2];
if (!brand) {
  console.error("Usage: npm run generate-icons -- /path/to/nbhd-ios/Brand");
  process.exit(1);
}

const publicDir = path.join(__dirname, "..", "public");
const files = [
  ["exports/favicon.ico", "favicon.ico"],
  ["exports/favicon-16.png", "icons/favicon/favicon-16x16.png"],
  ["exports/favicon-32.png", "icons/favicon/favicon-32x32.png"],
  ["exports/apple-touch-icon-180.png", "icons/apple-touch-icon.png"],
  ["exports/pwa-192.png", "icons/pwa/icon-192.png"],
  ["exports/pwa-512.png", "icons/pwa/icon-512.png"],
  ["exports/maskable-512.png", "icons/pwa/maskable-512.png"],
  ["exports/header-dark-64.png", "icons/brand/icon-white-64.png"],
  ["exports/header-light-64.png", "icons/brand/icon-green-64.png"],
  ["exports/email-icon-176.png", "images/email/nbhd-icon-176.png"],
  ["masters/nbhd-mark-small.svg", "icons/favicon/favicon.svg"],
  // Copy last: the artwork lane may refresh this export independently.
  ["exports/social-card-1200x630.png", "images/og/nbhd-social-card-1200x630.png"],
];

// Check the source set before changing any public files.
for (const [source] of files) fs.accessSync(path.join(brand, source), fs.constants.R_OK);

for (const [source, target] of files) {
  const destination = path.join(publicDir, target);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  if (target.endsWith(".svg")) {
    // Preserve approved geometry/colors; add a backing tile for light browser chrome.
    const svg = fs.readFileSync(path.join(brand, source), "utf8")
      .replace(/aria-label="[^"]*"/, 'aria-label="NBHD N mark"')
      .replace(/<title>.*?<\/title>/, '<title>NBHD N mark</title>\n  <rect width="32" height="32" fill="#0B0F13"/>');
    fs.writeFileSync(destination, svg);
  } else {
    fs.copyFileSync(path.join(brand, source), destination);
  }
  console.log(`Copied ${target}`);
}
