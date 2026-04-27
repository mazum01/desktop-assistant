# systemd integration — split-unit layout

The assistant runs as **two independent systemd units** so that a
failure in motion / audio / vision can never disrupt thermal safety.

| Unit | Process | Purpose | Restart policy |
|---|---|---|---|
| `desktop-assistant-thermal.service` | `python3 -m src.assistant.thermal_main` | TMP117 sensor + PWM fan loop | `Restart=always`, no rate limit |
| `desktop-assistant-core.service`    | `python3 -m src.assistant.core_main`    | Motion + AV (+ perception, dialog later) | `Restart=on-failure`, rate-limited |

Each process has its own in-process `MessageBus`. They don't share
events yet — when that's needed (Phase 3), we'll add an IPC transport
(likely ZeroMQ over a unix socket).

## Install

```bash
sudo cp services/systemd/desktop-assistant-thermal.service /etc/systemd/system/
sudo cp services/systemd/desktop-assistant-core.service    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now desktop-assistant-thermal.service
sudo systemctl enable --now desktop-assistant-core.service
```

`desktop-assistant-core` declares `Wants=desktop-assistant-thermal`,
so enabling core also pulls thermal up. It declares `After=` thermal,
so on boot thermal comes up first.

## Observe

```bash
systemctl status desktop-assistant-thermal desktop-assistant-core
journalctl -fu desktop-assistant-thermal
journalctl -fu desktop-assistant-core
journalctl -f -u desktop-assistant-thermal -u desktop-assistant-core   # interleaved
```

## Stop / disable

```bash
sudo systemctl stop  desktop-assistant-core desktop-assistant-thermal
sudo systemctl disable desktop-assistant-core desktop-assistant-thermal
```

## Notes

- Both units run as user `starter`, group `starter`, with supplementary
  groups for the hardware they touch (`i2c`, `gpio`, plus `audio`,
  `video` for the core unit). Make sure your user is a member.
- Path is hard-coded to `/home/starter/Code/Desktop Assistant` — edit
  the unit files if your layout differs.
- `ProtectSystem=strict` + `ReadWritePaths=/tmp` keeps the rest of the
  filesystem read-only. The camera test still writes to `/tmp/`.
