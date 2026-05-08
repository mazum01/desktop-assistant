---
name: restart-daemon
description: >
  Restart the desktop-assistant-core systemd service and confirm it is back up.
  USE FOR: applying code changes that require a daemon restart; user asks to
  restart the daemon; after modifying any service, config, or source file that
  needs a live reload. ALWAYS call this skill after making code changes to the
  running assistant, unless the user explicitly says not to.
---

# Restart Daemon Skill

## Steps

1. Run:
   ```bash
   sudo systemctl restart desktop-assistant-core.service
   ```

2. Wait ~3 seconds, then verify:
   ```bash
   systemctl is-active desktop-assistant-core.service
   ```

3. Confirm with a ping:
   ```bash
   da ping
   ```

4. Report the result to the user — either "Daemon restarted successfully" or the
   error output if the service failed to start (check `journalctl -u desktop-assistant-core -n 30`).

## Notes

- The restart is passwordless (`sudo -n` confirmed to work for `starter` user).
- After restart, services take ~5s to fully initialize before `da ping` is reliable.
- If `systemctl restart` fails, check logs: `journalctl -u desktop-assistant-core -n 50 --no-pager`
