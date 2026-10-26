import { readFile, mkdir, writeFile } from "node:fs/promises";
import { randomInt } from "node:crypto";
import { resolve, join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { Endpoint, ServerNode, VendorId } from "@matter/main";
import { BridgedDeviceBasicInformationServer } from "@matter/main/behaviors/bridged-device-basic-information";
import { OnOffPlugInUnitDevice } from "@matter/main/devices/on-off-plug-in-unit";
import { AggregatorEndpoint } from "@matter/main/endpoints/aggregator";
import { AudioClient, validateConfig } from "./audio-client.mjs";
import { createMuteServer } from "./mute-device.mjs";

const config = validateConfig(JSON.parse(await readFile(resolve(process.argv[2]), "utf8")));
const storagePath = resolve(process.env.MATTER_STORAGE_PATH);
const version = (await readFile(new URL("../../VERSION", import.meta.url), "utf8")).trim();
await mkdir(storagePath, { recursive: true, mode: 0o700 });
const credentialsPath = join(storagePath, "commissioning.json");
let credentials;
try {
  credentials = JSON.parse(await readFile(credentialsPath, "utf8"));
} catch (error) {
  if (error.code !== "ENOENT") throw error;
  // Avoid the reserved/repeated PIN values in the Matter specification.
  let passcode;
  do {
    passcode = randomInt(1, 99999999);
  } while (/^(\d)\1{7}$/.test(String(passcode).padStart(8, "0")) ||
           passcode === 12345678 || passcode === 87654321);
  credentials = { passcode, discriminator: randomInt(0, 4096) };
  await writeFile(credentialsPath, JSON.stringify(credentials), { mode: 0o600, flag: "wx" });
}

const audio = new AudioClient(config);
const AudioMuteServer = createMuteServer(audio);

const server = await ServerNode.create({
  id: config.bridgeId,
  network: { port: config.matterPort },
  commissioning: credentials,
  productDescription: { name: config.bridgeName, deviceType: AggregatorEndpoint.deviceType },
  basicInformation: {
    vendorId: VendorId(0xfff1),
    vendorName: "VERA",
    productId: 0x8000,
    productName: config.bridgeName,
    softwareVersionString: version,
    nodeLabel: config.bridgeName,
    serialNumber: config.bridgeId,
    uniqueId: config.bridgeId,
  },
});
const aggregator = new Endpoint(AggregatorEndpoint, { id: "devices" });
await server.add(aggregator);
const mute = new Endpoint(
  OnOffPlugInUnitDevice.with(BridgedDeviceBasicInformationServer, AudioMuteServer),
  {
    id: "audio-mute",
    bridgedDeviceBasicInformation: {
      nodeLabel: config.muteName,
      productName: config.muteName,
      uniqueId: `${config.bridgeId}-mute`,
      reachable: false,
    },
  },
);
await aggregator.add(mute);

async function synchronize() {
  // Hold the endpoint transaction while reading, so an older poll cannot
  // overwrite the result of a concurrent Matter command.
  await mute.act(async agent => {
    const onOff = agent.get(AudioMuteServer);
    const information = agent.get(BridgedDeviceBasicInformationServer);
    await agent.context.transaction.begin();
    await agent.context.transaction.addResources(onOff, information);
    const muted = await audio.request();
    onOff.state.onOff = muted;
    information.state.reachable = true;
  });
}

const shutdown = new AbortController();
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, () => shutdown.abort());
}
try {
  await synchronize();
} catch (error) {
  console.error("VERA audio unavailable at bridge startup:", error);
}
await server.start();
console.info("VERA Matter mute bridge ready; ON means audio muted (not microphone privacy).");
try {
  while (!shutdown.signal.aborted) {
    try {
      await synchronize();
    } catch (error) {
      console.error("VERA audio synchronization failed:", error);
      await mute.set({ bridgedDeviceBasicInformation: { reachable: false } });
    }
    await delay(config.pollIntervalMs, undefined, { signal: shutdown.signal });
  }
} catch (error) {
  if (error.name !== "AbortError") throw error;
} finally {
  await server.close();
}
