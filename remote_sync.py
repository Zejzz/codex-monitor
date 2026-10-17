"""Authenticated tailnet ingestion and durable device snapshots."""
import copy
from contextlib import closing
import hmac
import ipaddress
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 16 * 1024 * 1024
IDENTIFIER = re.compile(r'^[a-zA-Z0-9_-]{1,100}$')
ROW_FIELDS = ['id','title','name','cwd','source','model','reasoning_effort','agent_role',
              'agent_nickname','agent_path','creator_account_id']
TOKEN_KEYS = ['input_tokens','cached_input_tokens','cache_write_input_tokens',
              'output_tokens','reasoning_output_tokens','total_tokens']


def number(value, maximum=1e15):
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError('Invalid numeric telemetry')
    return value


def text(value, limit=1000):
    return str(value or '')[:limit]


def clean_thread(item):
    """Explicit whitelist: discard conversation text, instructions, credentials."""
    raw_row, raw_state = item['row'], item['state']
    if not isinstance(raw_row,dict) or not isinstance(raw_state,dict):
        raise ValueError('Thread metadata must be objects')
    for field in ['records','contexts','limits']:
        if not isinstance(raw_state.get(field,[]),list):
            raise ValueError('Telemetry history must be a list')
    if not isinstance(raw_state.get('turns',{}),dict):
        raise ValueError('Turns must be an object')
    identifier = raw_row['id']
    if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
        raise ValueError('Invalid thread ID')
    row = {key: text(raw_row.get(key)) for key in ROW_FIELDS}
    row.update(id=identifier, archived=bool(raw_row.get('archived')),
               created_at=number(raw_row.get('created_at',0),1e12),
               updated_at=number(raw_row.get('updated_at',0),1e12),
               tokens_used=int(number(raw_row.get('tokens_used',0))))
    state = dict(model=text(raw_state.get('model'),150) or 'unknown',
                 tier=text(raw_state.get('tier'),30) or 'unknown',
                 tier_presence=text(raw_state.get('tier_presence'),30) or 'absent',
                 effort=text(raw_state.get('effort'),50) or 'unknown',
                 parent=text(raw_state.get('parent'),100) or None,
                 latest=number(raw_state.get('latest',0),1e12), malformed=0,
                 records=[], contexts=[], limits=[], turns={})
    if len(raw_state.get('records',[])) > 100000:
        raise ValueError('Too many records in one thread')
    for raw in raw_state.get('records',[]):
        usage = {k:int(number(raw['usage'].get(k,0))) for k in TOKEN_KEYS}
        if usage['total_tokens'] != usage['input_tokens'] + usage['output_tokens']:
            raise ValueError('Token total does not match input plus output')
        state['records'].append(dict(time=number(raw['time'],1e12),model=text(raw['model'],150),
            effort=text(raw.get('effort'),50),tier=text(raw.get('tier'),30) or 'unknown',tier_presence=text(raw.get('tier_presence'),30) or ('absent' if raw.get('tier','unknown')=='unknown' else 'present'),tier_origin=text(raw.get('tier_origin'),30),turn_id=text(raw.get('turn_id'),100),usage=usage,source=text(raw.get('source'),30)))
    for raw in raw_state.get('contexts',[])[-10000:]:
        state['contexts'].append(dict(time=number(raw['time'],1e12),
                                     model=text(raw['model'],150),effort=text(raw['effort'],50),tier=text(raw.get('tier'),30) or 'unknown'))
    for raw in raw_state.get('limits',[])[-10000:]:
        state['limits'].append(dict(time=number(raw['time'],1e12),id=text(raw['id'],100),
            window_minutes=number(raw['window_minutes'],1e8),used_percent=number(raw['used_percent'],100),
            resets_at=number(raw['resets_at'],1e12)))
    for turn_id, raw in list(raw_state.get('turns',{}).items())[-10000:]:
        state['turns'][text(turn_id,100)] = dict(start=number(raw.get('start',0),1e12),
            end=number(raw['end'],1e12) if raw.get('end') is not None else None,
            status=text(raw.get('status'),30),
            duration=number(raw['duration']) if raw.get('duration') is not None else None)
    return dict(row=row,state=state)


class RemoteStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.devices, self.threads = {}, {}
        with closing(sqlite3.connect(self.path)) as con:
            con.execute('CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY,name TEXT,seen REAL)')
            con.execute('CREATE TABLE IF NOT EXISTS threads (device TEXT,id TEXT,revision REAL,payload TEXT,PRIMARY KEY(device,id))')
            con.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT)')
            con.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)',('epoch',str(uuid.uuid4())))
            self.epoch = con.execute("SELECT value FROM metadata WHERE key='epoch'").fetchone()[0]
            for device, name, seen in con.execute('SELECT * FROM devices'):
                self.devices[device] = dict(id=device,name=name,seen=seen)
            for device, identifier, revision, payload in con.execute('SELECT * FROM threads'):
                self.threads[(device,identifier)] = dict(revision=revision,**json.loads(payload))
            con.commit()

    def ingest(self, payload, device):
        if payload.get('version') != 1 or payload.get('device_id') != device:
            raise ValueError('Invalid device or protocol version')
        revision = number(payload['revision'],1e12)
        if revision > time.time() + 300:
            raise ValueError('Collector clock is more than five minutes ahead')
        if not isinstance(payload.get('threads'),list) or len(payload['threads']) > 100:
            raise ValueError('Upload at most 100 threads per batch')
        # Validate entire batch before mutating durable state.
        clean = [clean_thread(t) for t in payload['threads']]
        if len({t['row']['id'] for t in clean}) != len(clean):
            raise ValueError('Duplicate thread IDs in batch')
        name, seen = text(payload.get('device_name'),150) or device, time.time()
        accepted = []
        with self.lock, closing(sqlite3.connect(self.path)) as con:
            con.execute('INSERT OR REPLACE INTO devices VALUES (?,?,?)',(device,name,seen))
            for item in clean:
                key = (device,item['row']['id'])
                if revision < self.threads.get(key,{}).get('revision',0):
                    continue
                con.execute('INSERT OR REPLACE INTO threads VALUES (?,?,?,?)',
                            (*key,revision,json.dumps(item)))
                accepted.append((key,dict(revision=revision,**item)))
            con.commit()
            self.devices[device] = dict(id=device,name=name,seen=seen)
            self.threads.update(accepted)
        return dict(ok=True,accepted=len(accepted),epoch=self.epoch)

    def collection(self):
        with self.lock:
            return copy.deepcopy(self.devices), copy.deepcopy(self.threads)

    def snapshot_refs(self):
        """Internal read-only view. Ingest replaces whole items rather than mutating them.

        Readers must create their own derived records before changing usage/tier fields.
        Copying the mapping preserves its membership across concurrent uploads.
        """
        with self.lock:
            return copy.deepcopy(self.devices), dict(self.threads)


def make_receiver(store, config_path, bind, port):
    address = ipaddress.ip_address(bind)
    if not address.is_loopback and address not in ipaddress.ip_network('100.64.0.0/10'):
        raise ValueError('Receiver must bind to loopback or a Tailscale IPv4 address')
    class Receiver(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def authenticate(self):
            device = self.headers.get('X-Codex-Device-ID','')
            try:
                config = json.loads(Path(config_path).read_text(encoding='utf-8'))
                expected = config.get('device_tokens',{}).get(device)
                if expected and hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+expected):
                    return device
            except (OSError,ValueError,TypeError):
                pass
            return None

        def reply(self,status,payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            device=self.authenticate()
            if not device:
                return self.reply(401,dict(error='Valid device upload key required'))
            if self.path != '/api/health':
                return self.reply(404,dict(error='Upload endpoint only'))
            with store.lock:
                revision=max((item['revision'] for (owner,_),item in store.threads.items() if owner==device),default=0)
            return self.reply(200,dict(ok=True,epoch=store.epoch,revision=revision))

        def do_POST(self):
            device = self.authenticate()
            if not device:
                return self.reply(401,dict(error='Valid device upload key required'))
            if self.headers.get('Origin'):
                return self.reply(403,dict(error='Collectors only; browser uploads disabled'))
            if self.path != '/api/ingest':
                return self.reply(404,dict(error='Upload endpoint only'))
            try:
                size = int(self.headers.get('Content-Length',0))
                if not 0 < size <= MAX_BODY:
                    return self.reply(413,dict(error='Upload size limit exceeded'))
                self.connection.settimeout(20)
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ValueError('Incomplete upload')
                self.reply(200,store.ingest(json.loads(body),device))
            except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
                self.reply(400,dict(error='Invalid telemetry payload'))
            except OSError:
                self.reply(503,dict(error='Unable to persist telemetry; retry later'))
    return ThreadingHTTPServer((bind,port),Receiver)
