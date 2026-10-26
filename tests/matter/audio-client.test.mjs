import assert from "node:assert/strict";
import { test } from "node:test";
import { readFile } from "node:fs/promises";
import { AudioClient, validateConfig } from "../../src/matter/audio-client.mjs";

const config = JSON.parse(await readFile(new URL("../../config/matter.json", import.meta.url)));

test("configuration rejects remote URLs and invalid timing", () => {
  assert.equal(validateConfig(config), config);
  for (const change of [
    { audioMuteUrl: "http://example.com/api/audio/mute" },
    { audioMuteUrl: "http://user:password@127.0.0.1/api/audio/mute" },
    { pollIntervalMs: 0 }, { requestTimeoutMs: 1.5 }, { matterPort: 65536 },
    { muteName: "" },
  ]) {
    assert.throws(() => validateConfig({ ...config, ...change }));
  }
});

test("reads actual state and writes ON=muted without redirects", async () => {
  const requests = [];
  const client = new AudioClient(config, async (url, options) => {
    requests.push({ url, options });
    return Response.json({ ok: true, muted: options.method === "PUT" });
  });
  assert.equal(await client.request(), false);
  assert.equal(await client.request(true), true);
  assert.equal(requests[0].options.method, "GET");
  assert.equal(requests[1].options.body, '{"muted":true}');
  assert.equal(requests[1].options.redirect, "error");
  await assert.rejects(client.request("true"), /boolean/);
});

test("HTTP errors, malformed state, failed writes and transport errors propagate", async () => {
  for (const response of [
    new Response("unavailable", { status: 503 }),
    Response.json({ muted: "false" }),
    Response.json({ muted: false, ok: false }),
    Response.json({ muted: false, ok: true }),
    new Response("not JSON"),
  ]) {
    const client = new AudioClient(config, async () => response);
    await assert.rejects(client.request(true));
  }
  const client = new AudioClient(config, async () => { throw new Error("offline"); });
  await assert.rejects(client.request(), /offline/);
});
