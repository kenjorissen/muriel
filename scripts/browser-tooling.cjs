// Test-only installation boundary. No global module or system-browser fallback.
const { execFileSync } = require("node:child_process");
const { readFileSync } = require("node:fs");
const { createHash } = require("node:crypto");
const path = require("node:path");

const root = path.resolve(__dirname, "..");

function installationDirectory() {
  const common = execFileSync("git", ["-C", root, "rev-parse", "--path-format=absolute", "--git-common-dir"], { encoding: "utf8" }).trim();
  const digest = createHash("sha256");
  for (const file of ["package.json", "package-lock.json"]) {
    digest.update(readFileSync(path.join(root, "tools/browser", file)));
  }
  // Shared non-source development tooling, keyed by this worktree's manifests.
  // A changed pin cannot replace an installation used by another Candidate.
  return path.resolve(common, "../.venv/browser", digest.digest("hex"));
}

async function probe() {
  const expected = require(path.join(root, "tools/browser/package.json")).devDependencies.playwright;
  const directory = path.join(installationDirectory(), "node_modules/playwright");
  const actual = require(path.join(directory, "package.json")).version;
  if (actual !== expected) {
    throw new Error(`Expected Playwright ${expected}, found ${actual} at ${directory}`);
  }
  process.env.PLAYWRIGHT_BROWSERS_PATH = "0";
  const { chromium } = require(directory);
  const executable = chromium.executablePath();
  // Launch the same full Chromium executable used by the embedded runners.
  // This verifies OS libraries as well as the downloaded browser, offline.
  const browser = await chromium.launch({ headless: true, executablePath: executable });
  try {
    const page = await browser.newPage();
    await page.setContent("<!doctype html><title>Browser prerequisites</title>");
  } finally {
    await browser.close();
  }
  return { module: directory, executable };
}

if (process.argv[2] === "--installation-directory") {
  console.log(installationDirectory());
} else {
  probe().then((result) => console.log(JSON.stringify(result))).catch((error) => {
    console.error("Browser prerequisites unavailable. Run ./scripts/setup-browser in this checkout (Node 22+ and npm required; OS dependency installation may need sudo).");
    console.error(error.stack || error);
    process.exitCode = 1;
  });
}
