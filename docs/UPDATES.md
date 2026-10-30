# Approved releases and automatic device updates

Each device independently follows published stable releases from
`mazum01/desktop-assistant`. A push to `main` does **not** deploy it. Drafts and
prereleases are ignored. The updater never automatically downgrades.

## Body identity stays with the device

On original pan-only VERA hardware, create `config/body.yaml` containing
`body: original` before starting v1.75.0 or later. On the new pan/tilt body,
use `body: new`; an absent file retains new-body behavior with a warning.
This git-ignored file is preserved by checkout updates and included in the
updater's configuration backups. Never copy another body's identity or
body-tagged servo runtime state to a different device. See
[body profiles](FEATURES.md#body-profiles) for controller and wiring details.

## Development on VERA devices

Use one shared `main` branch and short-lived feature branches, not permanent
branches per robot. Develop in a separate worktree; do not edit the checkout
used by running services. For example, from the live checkout:

```bash
git fetch origin main
git worktree add -b feature/my-change ../desktop-assistant-my-change origin/main
```

The worktree does not inherit ignored machine-local configuration, credentials,
models, or calibration. Do not start its hardware services just to run unit
tests. Tests must use mocks and temporary configuration rather than production
devices or saved user settings.

Before activation, back up each device independently and explicitly confirm its
body identity. Preserve its runtime servo limits/channels, camera roles and
orientation, calibration, display address, and Hailo family/model selection.
Do not copy the other VERA's configuration. In particular, the new body's
default tilt limits are not a replacement for locally calibrated limits.
Body selection does not select cameras, accelerator drivers, or display firmware.

Open a PR and require tests for both body profiles and all affected components.
Merge the integrated branch, assign consistent release metadata, and publish
only the exact verified commit. A prerelease is suitable for an unvalidated
hardware candidate and is ignored by unattended device updates. Stable releases
require supervised validation on the affected hardware; roll out to one device
first, verify readiness and rollback, then the other.

Candidate tags use `vX.Y.Z-rc.N`; `VERSION` and the deployment manifest retain
the target `X.Y.Z`. Validate with the actual candidate tag, and publish with
GitHub's prerelease flag. Candidates must never be marked as the latest stable
release or installed by the unattended updater.

Confirm loaded version and revision through `deployment.readiness`, not only
the on-disk `VERSION`. Identify whether units run under `systemctl --user` or
the system manager before checking or restarting them. Application deployment
does not include display flashing or accelerator-family/driver migration.

## One-time installation on each existing device

Start with a reviewed updater-aware checkout (v1.64.0 or later). For this
first installation, obtain that revision using the existing manual deployment
process, install its requirements, and restart the VERA services so that every
IPC process supports `deployment.readiness`.

Run in the checkout, substituting the actual VERA account:

```bash
sudo python3 scripts/install_updater.py --user starter
systemctl status vera-updater.service vera-updater.timer --no-pager
python3 scripts/desktop-assistant update status
python3 scripts/desktop-assistant update check
```

The installer detects the checkout path, account, and core unit manager. It
installs an independent root-owned controller under `/opt/vera-updater` and
its policy under `/etc/desktop-assistant/updater-policy.json`. Transaction
state and private backups live under `/var/lib/vera-updater`. Existing VERA
services can be system, user, or mixed units; duplicate units or custom
`ExecStart` changes need manual resolution.
Deployed watchdog units receive per-service `VERA_SYSTEMD_SCOPE_*` overrides
so health checks target each service's actual manager, preserving legacy
defaults where no override is present.

The controller must have noninteractive network access. Public repositories
need no GitHub login. For a private repository, provision a **read-only**
fine-grained token with repository Contents and Metadata read access:

```bash
sudo install -m 0600 -o root -g root /dev/null \
  /etc/desktop-assistant/updater-github-token
sudoedit /etc/desktop-assistant/updater-github-token
```

Enter the token only in that protected file, never in source, command
arguments, chat, or release metadata. The updater does not use `gh`'s
interactive user keyring. An inaccessible repository is an error, not a
successful "no updates" response.

## Scheduling and manual requests

Open **System → Software Updates** in the dashboard. Set automatic updates
and start/end times. The default is **02:00-04:00 in each device's local
timezone**. Overnight windows are supported. Start and end must differ.
Polling occurs periodically, with release discovery throttled to hourly.
The window gates starting an installation; it does not interrupt an ongoing
package installation when the window ends.

For dashboard controls, set a top-level `api_key` in `config/assistant.yaml`
and restart `desktop-assistant-web.service`. On the next API request, the
dashboard prompts for that key. It sends the key in `X-API-Key` for API calls
and over the WebSocket, and stores it in this browser's `localStorage` as
`vera_api_key`. Use a unique random key; don't reuse a GitHub token. The key
protects API endpoints (the HTML shell and static assets remain public).
Updater endpoints refuse operation when no API key is configured, even if
the rest of the dashboard is otherwise unauthenticated.

On a VERA terminal, from the repository root, this command adds a random
32-byte key without rewriting the rest of the YAML file, restricts the config
file to its owner, and restarts the user web service. It prints the new key
once so you can enter it into the browser prompt:

```bash
python3 - <<'PY'
from pathlib import Path
import json
import re
import secrets

path = Path("config/assistant.yaml")
text = path.read_text()
if re.search(r"(?m)^api_key\s*:", text):
    raise SystemExit("api_key already exists; edit or rotate it deliberately")
key = secrets.token_urlsafe(32)
path.write_text(f"api_key: {json.dumps(key)}\n" + text)
path.chmod(0o600)
print("Copy this API key into the VERA dashboard prompt:")
print(key)
PY
systemctl --user restart desktop-assistant-web.service
```

Refresh the dashboard and enter the printed key when prompted. It will then be
remembered by that browser profile. To authorize another browser, copy the key
from the top-level `api_key` field on VERA and enter it there. The updater
itself does not need a GitHub token for this public repository.

```bash
vera update status
vera update check
vera update install
vera update install --retry
vera help update
```

Check/install requests are asynchronous. "Accepted" does not mean
"installed successfully." Status and the dashboard show durable progress,
pending reboot, history, errors, recovery, and required manual actions.
Manual installation bypasses the overnight window. Automatic scheduling
can be disabled without disabling manual controls.

## Release author responsibilities

`config/deployment.json` is the versioned deployment contract. It must match
the release tag and `VERSION`. Review it with every release:

- Supported OS/architecture and required APT/Python dependencies/imports.
- Required service templates and explicitly retired managed units.
- Required assets with SHA256 checksums and HTTPS source URLs.
- Explicit declarative configuration renames where defaults change shape.
- Boot prerequisites and reboot policy.
- Disk-space and health observation thresholds.

The shared APT package list is also used by `scripts/setup_pi.sh`. Drivers
required by a release must be declared in its APT requirements, reviewed for
the intended devices, and assigned the appropriate reboot policy. Do not
declare an unrelated full OS upgrade or an accelerator-family migration.
Existing optional Hailo deployments retain the intended CPU fallback.
Piper catalog assets are selected using each device's configured voice;
custom voices/models must already exist or be added to the manifest.
Voice configuration JSON is pinned by the release commit.

The manifest supports additive/default changes and explicit key renames in
`assistant.yaml` and `thermal.yaml`. Removed keys and type-changing migrations
without supported handling fail explicitly. Arbitrary database migrations,
firmware flashing, OpenClaw upgrades, and executable deployment hooks are not
supported by this schema. Do not publish a release requiring them for
automatic deployment until the contract/runner supports them.

After tests and hardware review, commit and push the source, version,
changelog, manifest, and architecture outputs. Validate, then publish:

```bash
python3 scripts/validate_release.py --tag v1.64.0
git rev-parse HEAD
gh release create v1.64.0 \
  --repo mazum01/desktop-assistant \
  --target THE_REVIEWED_FULL_COMMIT_SHA \
  --title "VERA v1.64.0" \
  --generate-notes
```

Replace the example version and commit. Publishing is the explicit approval
step. Do not move a published release tag. Legacy releases without a supported
manifest are not eligible for unattended installation.

## Deployment and safety

The controller stages the exact tagged revision without changing the live
checkout, verifies metadata/assets, checks platform/disk space and local
edits, and snapshots configuration and service definitions. Staged or edited
source files and untracked nonignored files block installation. Only the
tracked device-local assistant/thermal configuration and calibration are
allowed to differ; ignored runtime state and models stay local.

Application writers and the watchdog are stopped for activation. Required
global packages are installed, the native decoder is built, source advances
only by fast-forward (or a detached checkout), defaults are merged with local
choices, units/udev rules are updated, and services restart in dependency
order. User-directory writes run as the device account, not as root.
Secrets, face data, calibration, device identities, and OpenClaw state are
not copied from another device.

Thermal protection stays live during staging/dependency work. A thermal
restart requires verified **kernel sysfs PWM at full duty**. The controller
refuses unattended installation on simulated/unsupported fan backends rather
than claiming safe operation. Kernel PWM remains enabled at full duty when
the thermal process exits; servo motion is not used as a deployment test.
If a device currently uses only lgpio PWM, establish and verify kernel PWM
through the normal hardware bring-up procedure before unattended deployment.
If startup logs report `Permission denied` on a PWM attribute, check that
the thermal account belongs to `gpio` and that the system's udev PWM rule
grants that group write access. From v1.98.3, the fan waits briefly for
those permissions after exporting its PWM channel. On an already running
device where permissions are now correct, restarting the thermal service
restores kernel PWM; verify `backend=sysfs` in `vera status` before relying
on automatic updates. Persistent permission failures still select the
fallback and block unattended thermal restarts.

Required reboots are persisted before reboot and followed by readiness
verification. Success requires the new revision loaded by fresh processes,
stable managed units, IPC/web responsiveness, and fresh required thermal,
camera, and audio health—not just a changed `VERSION` or `/health`.

## Failure and recovery

Status/journal errors identify the failing phase. Recovery restores the
previous application revision, device configuration, managed unit files and
supported boot/config migrations, rebuilds the old native extension, and
checks readiness. Failed revisions are quarantined until an explicit retry.
Interrupted transactions resume verification after reboot or attempt
recovery; a manual-repair state blocks further automatic mutation.

**System-wide package/driver changes are not fully reversible.** There is no
filesystem snapshot or isolated Python environment. Recovery can fail after
a dependency/driver change; status explicitly requires manual repair rather
than reporting success. Keep a device backup before approving such releases.
Restoring a boot file does not undo a kernel/driver already loaded on reboot.

```bash
journalctl -u vera-updater.service --no-pager -n 100
systemctl status vera-updater.service vera-updater.timer --no-pager
vera update status
sudo systemctl disable --now vera-updater.timer
```

Disabling the timer stops future automatic checks, not a deployment already
in progress. Do not interrupt an APT operation or remove transaction state
to bypass recovery. Resolve manual-repair incidents locally with the protected
backup and journal before re-enabling updates.
