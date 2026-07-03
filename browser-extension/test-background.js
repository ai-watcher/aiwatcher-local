"use strict";

const assert = require("node:assert/strict");
const { discoverServer, resetServerCache } = require("./background.js");

async function main() {
  const requests = [];
  global.fetch = async (url) => {
    requests.push(String(url));
    const found = String(url).startsWith("http://127.0.0.1:8766/");
    return {
      ok: found,
      status: found ? 200 : 404,
      json: async () => ({ service: "aiwatcher-local", capabilities: ["preflight"] }),
    };
  };

  resetServerCache();
  const server = await discoverServer();
  assert.equal(server.baseUrl, "http://127.0.0.1:8766");
  assert.deepEqual(requests.slice(0, 2), [
    "http://127.0.0.1:8765/api/health",
    "http://127.0.0.1:8766/api/health",
  ]);

  requests.length = 0;
  const cached = await discoverServer();
  assert.equal(cached.baseUrl, "http://127.0.0.1:8766");
  assert.deepEqual(requests, ["http://127.0.0.1:8766/api/health"]);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
