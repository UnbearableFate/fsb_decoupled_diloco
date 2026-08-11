import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readdir, readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";

const apiManifest = JSON.parse(
  await readFile(
    new URL("../app/reference-data/api-manifest.json", import.meta.url),
    "utf8",
  ),
);
const pythonSourcePaths = (
  await readdir(new URL("../../fs_diloco/", import.meta.url), { recursive: true })
)
  .filter((path) => path.endsWith(".py"))
  .map((path) => `fs_diloco/${path}`)
  .sort();
const repositoryRoot = fileURLToPath(new URL("../..", import.meta.url));

let workerPromise;

const routes = [
  ["/", "Filesystem Decoupled DiLoCo"],
  ["/overview", "Overview"],
  ["/getting-started", "Getting Started"],
  ["/concepts", "Concepts"],
  ["/user-guide", "User Guide"],
  ["/architecture", "Architecture"],
  ["/reference", "Reference"],
  ["/experiments", "Experiments"],
];

async function render(pathname) {
  workerPromise ??= import(new URL("../dist/server/index.js", import.meta.url).href);
  const { default: worker } = await workerPromise;

  return worker.fetch(
    new Request(`http://localhost${pathname}`, {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

for (const [pathname, expectedHeading] of routes) {
  test(`server-renders ${pathname}`, async () => {
    const response = await render(pathname);
    assert.equal(response.status, 200);
    assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

    const html = await response.text();
    assert.match(html, new RegExp(expectedHeading.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    assert.match(html, /FS-DiLoCo/);
    assert.doesNotMatch(html, /codex-preview|Your site is taking shape|react-loading-skeleton/i);
  });
}

test("exposes every required documentation section from the landing page", async () => {
  const html = await (await render("/")).text();
  for (const label of [
    "Overview",
    "Getting Started",
    "Concepts",
    "User Guide",
    "Architecture",
    "Reference",
  ]) {
    assert.match(html, new RegExp(label));
  }
});

// Native anchors keep documentation navigation functional without vinext RSC prefetch.
test("renders internal navigation as native anchors", async () => {
  const html = await (await render("/")).text();
  assert.match(html, /<a[^>]+href="\/getting-started"/);
  assert.match(html, /<a[^>]+href="\/architecture"/);
  assert.match(html, /<a[^>]+href="\/reference"/);
});

// The generated manifest must maintain a one-to-one mapping from current Python files to API routes.
test("indexes every Python source file as a unique API module page", () => {
  assert.equal(apiManifest.modules.length, apiManifest.stats.modules);
  assert.deepEqual(
    apiManifest.modules.map((moduleDoc) => moduleDoc.sourcePath).sort(),
    pythonSourcePaths,
  );
  assert.equal(
    new Set(apiManifest.modules.map((moduleDoc) => moduleDoc.route)).size,
    apiManifest.modules.length,
  );
  assert.equal(
    new Set(apiManifest.modules.map((moduleDoc) => moduleDoc.sourcePath)).size,
    apiManifest.modules.length,
  );
  for (const moduleDoc of apiManifest.modules) {
    assert.match(moduleDoc.sourcePath, /^fs_diloco\/.+\.py$/);
    assert.match(moduleDoc.route, /^\/reference\/fs_diloco(?:\/[a-zA-Z0-9_]+)*$/);
  }
});

// Website-only commits must not change the revision used by generated Python source links.
test("pins generated source links to the latest fs_diloco commit", () => {
  const sourceRevision = execFileSync(
    "git",
    ["log", "-1", "--format=%H", "--", "fs_diloco"],
    { cwd: repositoryRoot, encoding: "utf8" },
  ).trim();
  assert.equal(apiManifest.sourceRevision, sourceRevision);
});

// Every generated route must render, so a valid source module cannot silently disappear from Reference.
test("server-renders every generated Python API module page", async () => {
  for (const moduleDoc of apiManifest.modules) {
    const response = await render(moduleDoc.route);
    assert.equal(response.status, 200, moduleDoc.route);
    const html = await response.text();
    assert.match(html, new RegExp(moduleDoc.module.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    assert.match(html, /查看完整源码/);
  }
});

// The requested hf_data example exercises class, member, function, and callable-contract rendering.
test("renders complete member details for the hf_data API example", async () => {
  const response = await render("/reference/fs_diloco/modeling/hf_data");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /fs_diloco\.modeling\.hf_data/);
  assert.match(html, />Batch</);
  assert.match(html, /build_indexed_batch_iterator/);
  assert.match(html, />Parameters</);
  assert.match(html, />Returns</);
  assert.match(html, />Raises</);
});
