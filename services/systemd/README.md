# systemd integration — split-unit layout

The assistant runs as **five independent systemd units** so that a failure
in one domain (motion/audio/vision, thermal safety, media, integrations, or
the web dashboard) can never disrupt the others.

| Unit | Process | Purpose | Restart policy |
|---|---|---|---|
| `desktop-assistant-thermal.service` | `python3 -m src.assistant.thermal_main` | TMP117 sensor + PWM fan loop | `Restart=always`, no rate limit |
| `desktop-assistant-core.service`    | `python3 -m src.assistant.core_main`    | Motion, AV, vision/perception, RoomService, PrivacyService, ObjectDetection (Hailo group) | `Restart=on-failure`, rate-limited |
| `desktop-assistant-media.service`   | `python3 -m src.assistant.media_main`   | Pandora (`MusicService`) + podcasts (`PodcastService`) | `Restart=on-failure`, rate-limited |
| `desktop-assistant-integrations.service` | `python3 -m src.assistant.integrations_main` | Telegram, notifications, clock announcements, IoT, Skills | `Restart=on-failure`, rate-limited |
| `desktop-assistant-web.service`     | `python3 -m src.assistant.web_main`      | FastAPI dashboard/API, camera streams, settings routes (`WebService`) | `Restart=on-failure`, rate-limited |

Each process has its own in-process `MessageBus`, bridged across process
boundaries via ZeroMQ (`IPCBridge`/`ProcessNode`/`IPCClient` — see
`docs/architecture/PROCESS_ISOLATION_PROPOSAL.md` for the full design and
phase-by-phase rollout history).

## Install

```bash
sudo cp services/systemd/desktop-assistant-thermal.service      /etc/systemd/system/
sudo cp services/systemd/desktop-assistant-core.service         /etc/systemd/system/
sudo cp services/systemd/desktop-assistant-media.service        /etc/systemd/system/
sudo cp services/systemd/desktop-assistant-integrations.service /etc/systemd/system/
sudo cp services/systemd/desktop-assistant-web.service          /etc/systemd/system/
sudo install -D -m 0644 services/udev/70-respeaker-flex-xvf.rules /etc/udev/rules.d/70-respeaker-flex-xvf.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --attr-match=idVendor=2886 --attr-match=idProduct=0022
sudo systemctl daemon-reload
sudo systemctl enable --now desktop-assistant-thermal.service
sudo systemctl enable --now desktop-assistant-core.service
sudo systemctl enable --now desktop-assistant-media.service
sudo systemctl enable --now desktop-assistant-integrations.service
sudo systemctl enable --now desktop-assistant-web.service
```

`desktop-assistant-core`/`-media`/`-integrations`/`-web` each declare
`Wants=`/`After=` the other units they depend on as an IPC upstream or RPC
target (see each unit file's `[Unit]` section for the exact list), so
enabling any one of them also pulls its dependencies up, in the right boot
order.

> **Production note (this box):** the files above are the canonical
> system-level units, but this box's passwordless sudo policy is scoped to
> a fixed command list (`systemctl restart` on core/thermal/openclaw-gateway
> only — not `daemon-reload`/`enable`/`cp` into `/etc/systemd/system/`).
> `desktop-assistant-media.service`, `desktop-assistant-integrations.service`,
> and `desktop-assistant-web.service` are therefore installed as **`--user`**
> units instead (`~/.config/systemd/user/`, `systemctl --user enable --now`,
> managed without sudo) — functionally identical (same `ExecStart`, env
> vars, IPC socket paths), just a different manager. `src/watchdog/
> watchdog.py`'s `ManagedService(..., user_unit=True)` flag tracks this per
> service so health checks/auto-restart query the right systemd manager.
> Only `desktop-assistant-thermal.service` and `desktop-assistant-core.service`
> are true system-level units here.

## Observe

```bash
systemctl status desktop-assistant-thermal desktop-assistant-core \
    desktop-assistant-media desktop-assistant-integrations desktop-assistant-web
journalctl -fu desktop-assistant-thermal
journalctl -fu desktop-assistant-core
journalctl -fu desktop-assistant-media
journalctl -fu desktop-assistant-integrations
journalctl -fu desktop-assistant-web
journalctl -f -u desktop-assistant-thermal -u desktop-assistant-core \
    -u desktop-assistant-media -u desktop-assistant-integrations -u desktop-assistant-web   # interleaved
```

## Stop / disable

```bash
sudo systemctl stop    desktop-assistant-web desktop-assistant-integrations \
    desktop-assistant-media desktop-assistant-core desktop-assistant-thermal
sudo systemctl disable desktop-assistant-web desktop-assistant-integrations \
    desktop-assistant-media desktop-assistant-core desktop-assistant-thermal
```

## OpenClaw gateway

`openclaw-gateway.service` is **not** managed from this directory. It is
installed and owned by the `openclaw` CLI itself as a per-user systemd unit
(`~/.config/systemd/user/openclaw-gateway.service`), via `openclaw daemon
install`. Use `systemctl --user restart openclaw-gateway.service` to manage
it — never install a system-wide unit of the same name here, since a
duplicate system-level unit fighting the CLI's user unit for port 18789
caused a real production incident (watchdog SIGKILLed the healthy
CLI-managed gateway because it queried the wrong systemd manager — see
CHANGELOG 1.46.4). A legacy hand-rolled system unit used to live at
`services/systemd/openclaw-gateway.service`; it has been removed from this
repo for that reason.

## Notes

- All units run as user `starter`, group `starter`, with supplementary
  groups for the hardware they touch (`i2c`, `gpio`, plus `audio`,
  `video`, and `plugdev` for the units that need them). Make sure your user
  is a member.
- ReSpeaker Flex XVF host-control requires the accompanying udev rule so the
  raw USB device node is group-accessible to `plugdev`.
- Path is hard-coded to `/home/starter/Code/Desktop Assistant` — edit
  the unit files if your layout differs.
- `ProtectSystem=strict` + `ReadWritePaths=/tmp` (plus each unit's other
  listed paths) keeps the rest of the filesystem read-only; `ProtectHome=false`
  leaves `/home/starter` itself writable as normal, which is how
  `config/assistant.yaml` persistence (settings PUT routes) and the
  `~/.local/share/desktop-assistant/faces.db` SQLite file stay writable
  without needing to be listed explicitly. The camera test still writes to
  `/tmp/`.
