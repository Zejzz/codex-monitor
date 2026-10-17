"""Prepare a paired collector bundle without printing its upload credential."""
import argparse
import ipaddress
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import uuid

ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name',default='Remote Codex')
    parser.add_argument('--bind')
    parser.add_argument('--port',type=int,default=8767)
    args=parser.parse_args()
    path=ROOT/'remote-config.json'
    if path.exists():
        config=json.loads(path.read_text(encoding='utf-8'))
    else:
        bind=args.bind
        if not bind:
            command=shutil.which('tailscale')
            if not command:raise SystemExit('Tailscale CLI unavailable; pass --bind with this PC\'s Tailscale IPv4 address.')
            bind=subprocess.check_output([command,'ip','-4'],text=True,timeout=10).strip().splitlines()[0]
        if ipaddress.ip_address(bind) not in ipaddress.ip_network('100.64.0.0/10'):
            raise SystemExit('Bind address must be in the Tailscale IPv4 range.')
        config=dict(bind=bind,port=args.port,device_tokens={})
    device=str(uuid.uuid4());token=secrets.token_urlsafe(32)
    config['device_tokens'][device]=token
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(config,indent=2),encoding='utf-8');temp.replace(path)
    bundle=ROOT/'collectors'/device
    bundle.mkdir(parents=True,exist_ok=False)
    for filename in ['collector.py','monitor.py','remote_sync.py','prices.json','install-linux-collector.sh']:
        shutil.copy2(ROOT/filename,bundle/filename)
    endpoint=config.get('endpoint') or f"http://{config['bind']}:{config['port']}"
    (bundle/'collector-config.json').write_text(json.dumps(dict(device_id=device,token=token,
        endpoint=endpoint,device_name=args.name,interval=10),indent=2),encoding='utf-8')
    (bundle/'Start Collector.cmd').write_text('@echo off\r\ncd /d "%~dp0"\r\npython collector.py\r\npause\r\n',encoding='utf-8')
    (bundle/'README.txt').write_text('Copy this WHOLE folder to the other Codex computer.\n'
        'Both computers must be online in the same Tailscale network.\n'
        'Requires Python 3.10+. Windows: double-click Start Collector.cmd.\n'
        'macOS/Linux: run python3 collector.py. Keep it running.\n'
        'Uses ~/.codex or CODEX_HOME; pass --home PATH for another location.\n'
        'This folder contains a device upload credential. Keep it private.\n'
        'It uploads usage, chat names, workspace paths and agent metadata; no message bodies or login tokens.\n'
        'Close the collector or press Ctrl+C to stop.\n',encoding='utf-8')
    archive=shutil.make_archive(str(bundle),'zip',root_dir=bundle)
    print('Receiver: '+endpoint+'/api/ingest')
    print('Collector bundle: '+archive)
    print('Restart the monitor after first-time setup. No restart is needed for additional paired devices.')


if __name__=='__main__':main()
