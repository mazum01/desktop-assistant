import { OnOffServer } from "@matter/main/behaviors/on-off";

export function createMuteServer(audio) {
  return class AudioMuteServer extends OnOffServer.with("Lighting") {
    async on() {
      await this.context.transaction.begin();
      await this.context.transaction.addResources(this);
      await audio.request(true);
      super.on();
    }
    async off() {
      await this.context.transaction.begin();
      await this.context.transaction.addResources(this);
      await audio.request(false);
      super.off();
    }
  };
}
