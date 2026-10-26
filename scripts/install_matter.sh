#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
node="$(command -v node)"
"$node" --input-type=module -e 'if (process.versions.node.split(".")[0] !== "24") throw Error("Node.js 24 is required")'
for value in "$root" "$HOME" "$node"; do
    if [[ "$value" == *'"'* || "$value" == *'\'* || "$value" == *$'\n'* ]]; then
        echo "Paths containing quotes, backslashes or newlines are not supported" >&2
        exit 1
    fi
done

npm --prefix "$root/src/matter" ci --omit=dev --no-audit --no-fund
state="$HOME/.local/share/vera-matter"
settings="$HOME/.config/vera-matter"
mkdir -p "$settings" "$state/controller" "$state/bridge" "$HOME/.config/systemd/user"
chmod 700 "$settings" "$state" "$state/controller" "$state/bridge"
umask 077
printf 'PATH="%s:/usr/local/bin:/usr/bin:/bin"\nVERA_MATTER_SOURCE="%s/src/matter"\nVERA_MATTER_CONFIG="%s/config/matter.json"\nVERA_MATTER_STATE="%s"\nMATTER_STORAGE_PATH="%s/bridge"\n' \
    "$(dirname "$node")" "$root" "$root" "$state" "$state" > "$settings/service.env"
install -m 644 "$root/services/systemd/vera-matter-controller.service" \
    "$root/services/systemd/vera-matter-bridge.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now vera-matter-controller.service vera-matter-bridge.service
systemctl --user is-active vera-matter-controller.service vera-matter-bridge.service
echo "Matter dashboard: http://127.0.0.1:5580 (use an SSH tunnel from another computer)"
