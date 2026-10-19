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

For web controls, set `api_key` in `config/assistant.yaml` and restart
`desktop-assistant-web.service`; the dashboard's existing authentication
mechanism (`X-API-Key` header or `?key=` query parameter) then covers the
updater endpoints too. Updater endpoints refuse operation when no API key is
configured, even if the rest of the dashboard is otherwise unauthenticated.

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
