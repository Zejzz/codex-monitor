#!/usr/bin/env bash
# Run as the Linux account whose Codex history should be collected.
set -euo pipefail
umask 077
bundle=$(cd -- "$(dirname -- "$0")" && pwd -P)
codex_home=${1:-${CODEX_HOME:-$HOME/.codex}}
install_dir="$HOME/.local/share/codex-usage-collector"
unit_dir="$HOME/.config/systemd/user"
unit_name=codex-usage-collector.service
command -v python3 >/dev/null
command -v systemctl >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ is required"'
test -d "$codex_home" || { echo "Codex home does not exist: $codex_home" >&2; exit 1; }
test -f "$bundle/collector-config.json"
if [[ -f "$unit_dir/$unit_name" && ! -f "$install_dir/collector-config.json" ]]; then
    echo "An existing collector service has no matching installation configuration. Installation stopped." >&2
    exit 1
fi
python3 - "$bundle/collector-config.json" "$install_dir/collector-config.json" <<'PY'
import json, pathlib, sys
source = json.loads(pathlib.Path(sys.argv[1]).read_text())
destination = pathlib.Path(sys.argv[2])
if destination.exists():
    prior = json.loads(destination.read_text())
    if prior['device_id'] != source['device_id']:
        raise SystemExit('An existing collector uses a different pairing. Installation stopped without replacing it.')
PY
# A lingering user manager starts at boot and survives logout.
if [[ $(loginctl show-user "$(id -u)" --property=Linger --value 2>/dev/null || true) != yes ]]; then
    if [[ $(id -u) == 0 ]]; then
        loginctl enable-linger "$(id -un)"
    else
        sudo -n loginctl enable-linger "$(id -un)" || {
            echo "Boot persistence requires an administrator to run: loginctl enable-linger $(id -un)" >&2
            exit 1
        }
    fi
fi
mkdir -p "$install_dir" "$unit_dir"
chmod 700 "$install_dir"
for file in collector.py monitor.py remote_sync.py prices.json collector-config.json; do
    if [[ "$bundle/$file" != "$install_dir/$file" ]]; then
        install -m 600 "$bundle/$file" "$install_dir/$file"
    fi
done
python3 - "$install_dir" "$codex_home" "$unit_dir/$unit_name" <<'PY'
import pathlib, shutil, sys
directory, home, unit = sys.argv[1:]
def quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
text = '\n'.join([
    '[Unit]', 'Description=Codex usage collector',
    'After=network-online.target', 'Wants=network-online.target', '',
    '[Service]', 'Type=simple',
    'WorkingDirectory=' + quote(directory),
    'ExecStart=' + quote(shutil.which('python3')) + ' -u ' + quote(directory + '/collector.py') + ' --home ' + quote(home),
    'Restart=always', 'RestartSec=15', 'UMask=0077',
    'NoNewPrivileges=true', 'PrivateTmp=true', '',
    '[Install]', 'WantedBy=default.target', '',
])
pathlib.Path(unit).write_text(text)
PY
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
systemctl --user daemon-reload
systemctl --user enable "$unit_name"
systemctl --user restart "$unit_name"
systemctl --user is-enabled "$unit_name"
systemctl --user is-active "$unit_name"
echo "Installed in $install_dir; service $unit_name starts at boot and restarts after failure."
echo "Logs: journalctl --user -u $unit_name -n 30 --no-pager"
