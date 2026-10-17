"""Run on another Codex computer; pushes telemetry to the monitor over Tailscale."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from monitor import Monitor


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def failure_description(error):
    """Report the failure without exposing URLs, credentials or response bodies."""
    if isinstance(error,urllib.error.HTTPError):
        explanation={401:'upload key rejected',403:'request forbidden',400:'telemetry rejected',
                     413:'upload too large',503:'receiver storage unavailable'}.get(error.code,'receiver HTTP error')
        return f'HTTP {error.code}: {explanation}'
    reason=error.reason if isinstance(error,urllib.error.URLError) else error
    if isinstance(reason,TimeoutError):return 'connection timed out'
    if isinstance(reason,ConnectionRefusedError):return 'connection refused; receiver may be stopped'
    if isinstance(reason,socket.gaierror):return 'hostname could not be resolved'
    if isinstance(error,ValueError):return 'invalid telemetry or collector configuration'
    return 'network failure ('+type(reason).__name__+')'


def validate_endpoint(endpoint):
    url=urlparse(endpoint)
    if url.username or url.password or url.query or url.fragment or url.path not in ['', '/']:
        raise ValueError('Endpoint must be a base URL without credentials, path, or query')
    host=url.hostname or ''
    try:
        address=ipaddress.ip_address(host)
        private=address.is_loopback or address in ipaddress.ip_network('100.64.0.0/10')
    except ValueError:
        private=host=='localhost'
    if not (url.scheme=='http' and private or url.scheme=='https' and (private or host.endswith('.ts.net'))):
        raise ValueError('Use a Tailscale IPv4 HTTP endpoint or a tailnet HTTPS .ts.net URL')
    return endpoint.rstrip('/')


class Uploader:
    def __init__(self,config):
        self.endpoint=validate_endpoint(config['endpoint'])
        self.device=config['device_id']
        self.name=config.get('device_name') or socket.gethostname()
        self.headers={'Authorization':'Bearer '+config['token'],'X-Codex-Device-ID':self.device,
                      'Content-Type':'application/json'}
        self.sent={}
        self.epoch=None
        self.last_revision=0
        # Tailnet traffic stays direct, and credentials never follow redirects.
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def request(self,path,payload=None):
        request=urllib.request.Request(self.endpoint+path,
            data=json.dumps(payload,separators=(',',':')).encode() if payload is not None else None,
            headers=self.headers)
        with self.opener.open(request,timeout=20) as response:
            return json.load(response)

    def sync(self,items):
        health=self.request('/api/health')
        self.last_revision=max(self.last_revision,health.get('revision',0))
        if health['epoch']!=self.epoch:
            self.sent={}
            self.epoch=health['epoch']
        pending=[]
        for item in items:
            encoded=json.dumps(item,sort_keys=True,separators=(',',':')).encode()
            digest=hashlib.sha256(encoded).hexdigest()
            if self.sent.get(item['row']['id'])!=digest:
                if len(encoded)>12*1024*1024:
                    raise ValueError('A thread exceeds the 12 MiB per-thread upload limit')
                pending.append((item,digest,len(encoded)))
        uploaded=0
        while pending:
            batch=[];size=0
            while pending and len(batch)<50 and (not batch or size+pending[0][2]<12*1024*1024):
                item=pending.pop(0);batch.append(item);size+=item[2]
            self.last_revision=max(time.time(),self.last_revision+.001)
            payload=dict(version=1,device_id=self.device,device_name=self.name,revision=self.last_revision,
                         threads=[item[0] for item in batch])
            result=self.request('/api/ingest',payload)
            if not result.get('ok') or result.get('accepted')!=len(batch):
                raise ValueError('Receiver did not acknowledge the upload')
            for item,digest,_ in batch:self.sent[item['row']['id']]=digest
            uploaded+=len(batch)
        # Heartbeats let the dashboard mark silent/disconnected devices as stale.
        self.request('/api/ingest',dict(version=1,device_id=self.device,device_name=self.name,
                                      revision=time.time(),threads=[]))
        return uploaded


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=str(Path(__file__).with_name('collector-config.json')))
    parser.add_argument('--home',default=None)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    root=Path(args.config).resolve().parent
    import os
    home=args.home or config.get('codex_home') or os.environ.get('CODEX_HOME') or str(Path.home()/'.codex')
    monitor=Monitor(home,root/'.collector-cache.sqlite')
    thread=threading.Thread(target=monitor.run,daemon=True);thread.start()
    uploader=Uploader(config)
    print('Sending Codex usage from '+uploader.name+' to '+uploader.endpoint,flush=True)
    print('Keep this collector running to sync new chats and usage.',flush=True)
    try:
        while monitor.data['indexing']:
            if monitor.data['errors']:
                raise RuntimeError(monitor.data['errors'][0])
            time.sleep(.5)
        while True:
            try:
                count=uploader.sync(monitor.export_for_sync())
                print(time.strftime('%H:%M:%S')+f' · {count} changed chats uploaded',flush=True)
                if args.once:return
                time.sleep(max(5,int(config.get('interval',10))))
            except (OSError,ValueError,urllib.error.URLError) as e:
                # Print the failure class without echoing credentials or payloads.
                print('Sync failed: '+failure_description(e)+'. Retrying in 15 seconds.',flush=True)
                if args.once:raise SystemExit(1)
                time.sleep(15)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop.set();thread.join(timeout=5)


if __name__=='__main__':main()
