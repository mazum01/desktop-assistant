export function validateConfig(config) {
  for (const key of ["bridgeId", "bridgeName", "muteName"]) {
    if (typeof config[key] !== "string" || !config[key].trim() || config[key].length > 32) {
      throw new Error(`${key} must be a nonempty string of at most 32 characters`);
    }
  }
  for (const key of ["matterPort", "pollIntervalMs", "requestTimeoutMs"]) {
    if (!Number.isSafeInteger(config[key]) || config[key] < 1) {
      throw new Error(`${key} must be a positive integer`);
    }
  }
  if (config.matterPort > 65535) throw new Error("matterPort must be at most 65535");
  const url = new URL(config.audioMuteUrl);
  if (url.protocol !== "http:" || !["127.0.0.1", "[::1]"].includes(url.hostname) ||
      url.username || url.password || url.search || url.hash) {
    throw new Error("audioMuteUrl must be a loopback HTTP URL without credentials or query");
  }
  return config;
}

export class AudioClient {
  constructor(config, fetchImpl = fetch) {
    this.config = validateConfig(config);
    this.fetch = fetchImpl;
  }

  async request(muted) {
    const writing = muted !== undefined;
    if (writing && typeof muted !== "boolean") throw new Error("muted must be boolean");
    const response = await this.fetch(this.config.audioMuteUrl, {
      method: writing ? "PUT" : "GET",
      headers: writing ? { "Content-Type": "application/json" } : {},
      body: writing ? JSON.stringify({ muted }) : undefined,
      signal: AbortSignal.timeout(this.config.requestTimeoutMs),
      redirect: "error",
    });
    if (!response.ok) throw new Error(`VERA mute API returned HTTP ${response.status}`);
    const result = await response.json();
    if (typeof result?.muted !== "boolean" || (writing && result.ok !== true)) {
      throw new Error("VERA mute API returned an invalid response");
    }
    if (writing && result.muted !== muted) throw new Error("VERA did not apply requested mute state");
    return result.muted;
  }
}
