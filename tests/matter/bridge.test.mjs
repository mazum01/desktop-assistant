import assert from "node:assert/strict";
import { test } from "node:test";
import { Endpoint, ServerNode } from "../../src/matter/node_modules/@matter/main/dist/esm/index.js";
import { OnOffPlugInUnitDevice } from "../../src/matter/node_modules/@matter/node/dist/esm/devices/on-off-plug-in-unit.js";
import { createMuteServer } from "../../src/matter/mute-device.mjs";

test("Matter endpoint supports asynchronous commands and transactional synchronization", { timeout: 15000 }, async () => {
  const writes = [];
  let fail = false;
  const MuteServer = createMuteServer({
    async request(muted) {
      if (fail) throw new Error("backend unavailable");
      writes.push(muted);
      return muted;
    },
  });
  const node = await ServerNode.create({ id: "vera-matter-unit-test" });
  try {
    const endpoint = new Endpoint(OnOffPlugInUnitDevice.with(MuteServer), { id: "test-mute" });
    await node.add(endpoint);
    await endpoint.act(async agent => {
      await agent.get(MuteServer).on();
      assert.equal(agent.get(MuteServer).state.onOff, true);
    });
    await endpoint.act(agent => agent.get(MuteServer).toggle());
    assert.deepEqual(writes, [true, false]);
    fail = true;
    await assert.rejects(endpoint.act(agent => agent.get(MuteServer).on()), /backend unavailable/);
    assert.equal(endpoint.state.onOff.onOff, false);
    await endpoint.set({ onOff: { onOff: true } });
    assert.deepEqual(writes, [true, false], "polling must not issue another write");
    fail = false;
    await Promise.all([
      endpoint.act(agent => agent.get(MuteServer).on()),
      endpoint.act(agent => agent.get(MuteServer).off()),
    ]);
    assert.deepEqual(writes, [true, false, true, false]);
  } finally {
    await node.close();
  }
});
