# Matter on VERA

VERA can host two optional, independent Node.js 24 user services:

- **vera-matter-controller**: the Open Home Foundation `matter-server`, with
  its dashboard and WebSocket API bound to `127.0.0.1:5580`.
- **vera-matter-bridge**: a Matter bridge exposing **VERA Audio Mute**.
  Switch **on = output audio muted**, **off = unmuted**. This is not microphone
  mute or privacy mode. Changes made in VERA are reflected back into Matter.
  The endpoint is marked unreachable if polling the VERA API fails; command
  failures are returned to the Matter controller rather than accepted locally.

These are not Thread border routers. The Tapo P316M uses Matter over Wi-Fi
and needs no extra Thread radio. Thread devices require an existing compatible
border router or a separate supported radio/OTBR deployment.

## Install

Install Node.js **24** and npm using a trusted distribution, put them on `PATH`,
then run from the repository:

```bash
bash scripts/install_matter.sh
```

No root access or Docker is required. The installer uses the committed npm
lockfile and creates user services, a private environment file under
`~/.config/vera-matter/`, and private persistent state under
`~/.local/share/vera-matter/{controller,bridge}`. Do not commit, share, or delete
this state: it contains fabric keys and bridge commissioning credentials.
Back it up securely; deleting it means pairing devices again.

User services must survive logout/reboot. Check `loginctl show-user "$USER"
-p Linger`; an administrator may need to run `loginctl enable-linger USER`.
The VERA web/media services must already be installed and running for mute.
Configure the loopback API URL, device names, polling, and bridge port in
[`config/matter.json`](../config/matter.json). Restart the bridge after edits.
The controller and bridge have separate storage and Matter ports.
The web API's strict mute RPC budget is configured by
`web_dashboard.mute_rpc_timeout_ms` in `config/assistant.yaml` (5000 ms in the
shipped configuration). The bridge HTTP budget must cover both write and
read-back RPCs; it defaults to 15000 ms. Other media proxy deadlines are
unchanged.

## Pair the P316M with VERA and Google Home

1. Set up the P316M's Wi-Fi in the Tapo app and apply its firmware updates.
   Keep the phone, VERA, and Google Home controller on the same LAN with IPv6
   and mDNS/multicast available. Do not expose dashboard port 5580 publicly.
2. Open VERA's Matter dashboard locally at `http://127.0.0.1:5580`.
   From another computer use an SSH tunnel:
   `ssh -L 5580:127.0.0.1:5580 starter@VERA_HOST`, then open that URL locally.
3. Open a Matter pairing window from the strip's existing controller/Tapo app
   and commission it using the dashboard. Use the newly generated sharing
   code if the strip is already commissioned, not its original label code.
   The exact app labels vary by version.
4. Use the existing controller's **share/add another ecosystem** function to
   open another pairing window. In Google Home, add a Matter-enabled device
   with that sharing code. Google needs a compatible Matter controller.
5. Confirm the strip's supported individual outlet endpoints appear, rename
   them, and verify each outlet from both controllers with a safe test load.
   Do not test by switching an outlet powering VERA, network equipment, or
   other essential equipment.

The outlets are shared **natively**, not duplicated as VERA bridge proxies.
Google retains control if VERA is offline. USB ports/energy reporting depend
on what the strip firmware exposes over Matter, not what the Tapo app offers.
VERA's controller dashboard is the initial control surface; this does not add
outlet commands to VERA's voice pipeline or main web dashboard.

## Pair VERA Audio Mute with Google Home

Inspect the bridge commissioning instructions locally:

```bash
journalctl _SYSTEMD_USER_UNIT=vera-matter-bridge.service -b --no-pager
```

The Matter runtime prints a QR/manual pairing code during its initial pairing
window. Treat this as a credential. Add it in Google Home as a Matter device.
This is a development bridge using test vendor ID **0xFFF1** and product ID
**0x8000**, not a CSA-certified product. Configure a matching Matter test
integration in the [Google Home Developer Console](https://console.home.google.com/)
for the Google account used on the phone; do not assume a normal consumer
pairing flow will accept this device. See Google's
[Matter development setup](https://developers.home.google.com/matter/get-started).
Ecosystem acceptance must be checked on the actual phone/controller.

The VERA controller also rejects development certificates by default. If the
bridge has already been commissioned into VERA, open its **commissioning
window/share device** action in the dashboard to obtain a fresh Google Home
pairing code. The original initial-pairing code cannot be used for multi-admin.
Accept development certificates only deliberately for devices you trust;
test-net DCL support is not enabled in the installed controller service.

After pairing, test **on**, **off**, and a mute change made in VERA. ON mutes
output audio; it does not disable listening. Do not assume successful pairing
until Google Home has completed it and an actual command has been verified.

## Operations

```bash
systemctl --user status vera-matter-controller vera-matter-bridge
systemctl --user restart vera-matter-controller vera-matter-bridge
journalctl _SYSTEMD_USER_UNIT=vera-matter-controller.service + _SYSTEMD_USER_UNIT=vera-matter-bridge.service -b
cd src/matter && npm test
```

Stop/disable with `systemctl --user disable --now vera-matter-controller
vera-matter-bridge`. Keep the private state to preserve pairings. Service restarts
and npm upgrades do not require resetting any device. Pairing windows expire;
reopening them must not involve deleting fabric state or factory-resetting an
already paired device.

Upstream: [OHF Matter Server](https://github.com/matter-js/matterjs-server) and
[Matter.js](https://github.com/matter-js/matter.js). The former Python Matter
Server is retired; this setup uses its maintained Node.js replacement.
