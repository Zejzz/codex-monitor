"""Read-only Codex telemetry monitor. No dependencies or API credentials required."""
import argparse
from contextlib import closing
import copy
import datetime as dt
import gzip
import json
import os
from pathlib import Path
import re
import sqlite3
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from remote_sync import RemoteStore, clean_thread, make_receiver

ROOT = Path(__file__).resolve().parent
KEYS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
        'output_tokens', 'reasoning_output_tokens', 'total_tokens')
TYPE = re.compile(rb'"type"\s*:\s*"([^"]+)"')


def timestamp(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 10**12 else value
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def readonly(path):
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    return con


def blank():
    return dict(version=4, offset=0, model='unknown', effort='unknown', tier='unknown', tier_presence='absent', baseline={}, pending={}, records=[],
                seen=[], turns={}, limits=[], parent=None, history_start=0,
                inherited_before=0, malformed=0, contexts=[], latest=0)


def usage(value):
    result = {k: max(0, int(value.get(k) or 0)) for k in KEYS}
    if not result['total_tokens']:
        result['total_tokens'] = result['input_tokens'] + result['output_tokens']
    return result


def tier_metadata(payload, fallback=None):
    for key in ['service_tier','speed','fast_mode']:
        if key not in payload:
            continue
        value=payload[key]
        presence='null' if value is None else 'empty' if value=='' else 'present'
        if key=='fast_mode' and isinstance(value,bool):value='fast' if value else 'standard'
        return dict(tier=str(value).lower() if presence=='present' else 'unknown',tier_presence=presence)
    return {k:fallback.get(k,'unknown' if k=='tier' else 'absent') for k in ['tier','tier_presence']} if fallback else dict(tier='unknown',tier_presence='absent')


def speed_category(record):
    presence=record.get('tier_presence','absent' if record.get('tier','unknown')=='unknown' else 'present')
    if presence!='present':return presence if presence in ['absent','null','empty'] else 'absent'
    value=record.get('tier','unknown')
    return 'fast' if value in ['fast','priority'] else 'standard' if value in ['standard','default'] else 'other'


def diagnostic_tier(body):
    """Parse only structured feedback tags, never strings in user input logs."""
    try:
        pos=body.index('tags_json=')
        tags,_=json.JSONDecoder().raw_decode(body[pos+10:])
        if isinstance(tags,str):tags=json.loads(tags)
        turns=re.findall(r'turn_id=([^\s,}]+)',body[:pos])
        models=re.findall(r'model=([^\s,}]+)',body[:pos])
        if not turns or not isinstance(tags,dict) or 'service_tier' not in tags:return None
        return dict(turn_id=turns[-1].strip('"'),model=models[-1].strip('"') if models else 'unknown',
                    **tier_metadata(tags))
    except (ValueError,TypeError):return None


def recover_tiers(state, thread_id, evidence):
    records=[]
    for raw in state.get('records',[]):
        record=dict(raw)
        if speed_category(raw)=='absent' and raw.get('turn_id'):
            matching=[e for e in evidence.get((thread_id,raw['turn_id']),[])
                      if e['time']<=raw['time'] and e['model']==raw['model']]
            if matching:
                item=max(matching,key=lambda e:e['time'])
                record.update(tier=item['tier'],tier_presence=item['tier_presence'],tier_origin='diagnostic_selected')
        records.append(record)
    return dict(state,records=records)


def consume(state, event, thread_id):
    kind, p = event.get('type'), event.get('payload') or {}
    ts = timestamp(event.get('timestamp'))
    state['latest'] = max(state['latest'], ts)
    if kind == 'session_meta':
        state['parent'] = p.get('parent_thread_id')
        state['history_start'] = p.get('subagent_history_start_ordinal', 0)
        if p.get('forked_from_id'):
            state['inherited_before'] = timestamp(p.get('timestamp'))
        return
    inherited = (event.get('ordinal', 10**18) < state['history_start'] or
                 ts < state['inherited_before'])
    if kind == 'turn_context':
        state['turn_id']=p.get('turn_id')
        settings = (p.get('collaboration_mode') or {}).get('settings') or {}
        state['model'] = p.get('model') or settings.get('model') or 'unknown'
        state['effort'] = p.get('effort') or settings.get('reasoning_effort') or 'unknown'
        state.update(tier_metadata(p))
        if not inherited:
            context = dict(time=ts, model=state['model'], effort=state['effort'], tier=state['tier'])
            if not state['contexts'] or any(context[k] != state['contexts'][-1].get(k) for k in ['model','effort','tier']):
                state['contexts'].append(context)
        return
    if kind == 'token_usage_record':
        # Modern records identify ownership, even when a fork copies parent history.
        if inherited or p.get('thread_id', thread_id) != thread_id:
            return
        identity = p.get('response_id')
        if identity and identity in state['seen']:
            return
        if identity:
            state['seen'].append(identity)
        value = usage(p.get('usage') or {})
        if value['total_tokens']:
            state['records'].append(dict(time=ts, model=p.get('model') or state['model'],
                                        effort=state['effort'], turn_id=p.get('turn_id') or state.get('turn_id'), **tier_metadata(p,state), usage=value, source='request'))
        # Request thread totals can restart after an app migration while token_count
        # totals continue. Reconcile raw increments against the separate counter.
        state['pending'] = {k: state['pending'].get(k, 0) + value[k] for k in KEYS}
        return
    if kind != 'event_msg':
        return
    event_type = p.get('type')
    if event_type == 'token_count':
        info = p.get('info') or {}
        total = info.get('total_token_usage')
        if total:
            total = usage(total)
            prev = state['baseline']
            # Counter resets occur at compaction / migrations. Use last request then.
            reset = total['total_tokens'] < prev.get('total_tokens', 0)
            delta = usage(info.get('last_token_usage') or total) if reset else {
                k: max(0, total[k] - prev.get(k, 0)) for k in KEYS}
            state['baseline'] = total
            pending = state['pending']
            state['pending'] = {k: max(0, pending.get(k, 0) - delta[k]) for k in KEYS}
            delta = {k: max(0, delta[k] - pending.get(k, 0)) for k in KEYS}
            if not inherited and delta['total_tokens']:
                state['records'].append(dict(time=ts, model=state['model'], effort=state['effort'],
                                            turn_id=p.get('turn_id') or state.get('turn_id'), **tier_metadata(p,state), usage=delta, source='counter'))
        if not inherited and p.get('rate_limits'):
            limits = p['rate_limits']
            for name in ['primary', 'secondary']:
                window = limits.get(name)
                if window:
                    state['limits'].append(dict(time=ts, id=limits.get('limit_id') or 'codex',
                                               **window))
        return
    if inherited:
        return
    turn_id = p.get('turn_id') or 'legacy'
    if event_type == 'task_started':
        state['turns'][turn_id] = dict(start=timestamp(p.get('started_at')) or ts,
                                      end=None, status='inProgress')
    elif event_type in ['task_complete', 'task_completed', 'turn_aborted']:
        turn = state['turns'].get(turn_id)
        if turn:
            turn.update(end=ts, status='interrupted' if event_type == 'turn_aborted' else 'completed')


def read_increment(path, state, thread_id):
    stat = path.stat()
    if stat.st_size < state['offset']:
        state = blank()
    with path.open('rb') as stream:
        stream.seek(state['offset'])
        while True:
            before = stream.tell()
            line = stream.readline()
            if not line or not line.endswith(b'\n'):
                state['offset'] = before  # Retry partial writes on the next refresh.
                break
            match = TYPE.search(line[:300])
            if not match or match[1] not in [b'session_meta', b'turn_context', b'token_usage_record', b'event_msg']:
                continue
            if match[1] == b'event_msg' and not any(v in line[:450] for v in
                    [b'"token_count"', b'"task_started"', b'"task_complete"', b'"task_completed"', b'"turn_aborted"']):
                continue
            try:
                consume(state, json.loads(line), thread_id)
            except (ValueError, TypeError, KeyError):
                state['malformed'] += 1
    return state


def pricing_model(record, prices):
    rate = prices.get(record['model'])
    if rate and rate.get('history'):
        applicable = [r for r in rate['history'] if r['from'] <= record['time']]
        return max(applicable,key=lambda r:r['from'])['model'] if applicable else record['model']
    return record['model']


def resolved_rate(record, prices):
    return prices.get(pricing_model(record,prices))


def cache_break_reason(previous, current, prices):
    old,new=pricing_model(previous,prices),pricing_model(current,prices)
    if 'unknown' in [old,new]:return None
    if previous['model']!=current['model'] or old!=new:return 'Model change'
    before,after=previous.get('effort','unknown'),current.get('effort','unknown')
    generation=re.match(r'^gpt-(\d+)(?:[.-]|$)',new)
    if generation and int(generation[1])<6 and before!=after and 'unknown' not in [before,after]:
        return 'GPT-5 or earlier effort change'
    return None


def cache_pricing_states(rows, states, prices, policy='recorded'):
    """Annotate transitions; the optional scenario removes read discounts only.

    Never copy parent usage into a child, invent cache writes, or mutate stored logs.
    A transition affects the first owned usage record, not all later warmed requests.
    """
    result={}
    for row in rows:
        state=states.get(row['id'],{}).get('state',blank())
        records=state.get('records',[])
        previous=None
        parent=state.get('parent')
        if parent and records:
            parent_state=states.get(parent,{}).get('state',{})
            at=row.get('created_at') or records[0]['time']
            candidates=[r for r in parent_state.get('records',[]) if r['time']<=at]
            candidates += [dict(c,usage={}) for c in parent_state.get('contexts',[]) if c['time']<=at]
            previous=max(candidates,key=lambda r:r['time'],default=None)
        annotated=[]
        for raw in sorted(records,key=lambda r:r['time']):
            record=dict(raw)
            reason=cache_break_reason(previous,record,prices) if previous else None
            record['cache_break']=reason
            record['cache_break_from_parent']=bool(reason and not annotated and parent)
            record['cache_adjusted_tokens']=0
            if reason and policy=='switches':
                removed=raw['usage']['cached_input_tokens']
                record['usage']=dict(raw['usage'],cached_input_tokens=0)
                record['cache_adjusted_tokens']=removed
            annotated.append(record)
            previous=raw
        result[row['id']]=dict(state=dict(state,records=annotated))
    return result


def price_record(record, prices):
    rate = resolved_rate(record,prices)
    if not rate:
        return None
    tier = record.get('tier') or 'unknown'
    tier = {'priority':'fast','default':'standard','auto':'unknown'}.get(tier,tier)
    if tier not in ['standard','unknown']:
        rate = rate.get('tiers',{}).get(tier)
        if not rate:
            return None
    u = record['usage']
    long = bool(rate.get('long_threshold') and u['input_tokens'] > rate['long_threshold'])
    multiplier = 2 if long else 1
    output_multiplier = 1.5 if long else 1
    # Cached and cache-write input are components of input, reasoning is part of output.
    uncached = max(0, u['input_tokens'] - u['cached_input_tokens'] - u['cache_write_input_tokens'])
    return (multiplier * (uncached * rate['input'] + u['cached_input_tokens'] * rate['cached'] +
            u['cache_write_input_tokens'] * rate['write']) +
            output_multiplier * u['output_tokens'] * rate['output']) / 1e6


def summarize_models(rows, states, prices, start=0, end=float('inf')):
    """Aggregate owned records, never family totals, within [start, end)."""
    models = {}
    for row in rows:
        state = states.get(row['id'], {}).get('state', {})
        for record in state.get('records', []):
            if not start <= record['time'] < end:
                continue
            model = record['model']
            group = models.setdefault(model, dict(model=model, tokens=0, input=0, cached=0,
                output=0, reasoning=0, write=0, requests=0, cost=0, unpriced_tokens=0,
                first=record['time'], last=record['time'], efforts={}, threads={}))
            u = record['usage']
            cost = price_record(record, prices)
            group['tokens'] += u['total_tokens']
            group['requests'] += 1
            group['first'] = min(group['first'], record['time'])
            group['last'] = max(group['last'], record['time'])
            for short, full in [('input','input_tokens'),('cached','cached_input_tokens'),
                                ('output','output_tokens'),('reasoning','reasoning_output_tokens'),
                                ('write','cache_write_input_tokens')]:
                group[short] += u[full]
            group['cost'] += cost or 0
            group['unpriced_tokens'] += u['total_tokens'] if cost is None else 0
            effort = group['efforts'].setdefault(record['effort'], dict(
                effort=record['effort'], tokens=0, cost=0, requests=0, unpriced_tokens=0))
            contribution = group['threads'].setdefault(row['id'], dict(
                id=row['id'], title=row.get('name') or row['title'] or row['id'],
                tokens=0, cost=0, requests=0, unpriced_tokens=0, last=0))
            for target in [effort, contribution]:
                target['tokens'] += u['total_tokens']
                target['cost'] += cost or 0
                target['requests'] += 1
                target['unpriced_tokens'] += u['total_tokens'] if cost is None else 0
            contribution['last'] = max(contribution['last'], record['time'])
    for group in models.values():
        group['efforts'] = sorted(group['efforts'].values(), key=lambda x: -x['tokens'])
        group['threads'] = sorted(group['threads'].values(), key=lambda x: -x['tokens'])
    return sorted(models.values(), key=lambda x: -x['tokens'])


def models_from_threads(threads):
    """Reuse own-request groups already priced for the same dashboard snapshot."""
    models={}
    for thread in threads:
        for own in thread['groups']:
            group=models.setdefault(own['model'],dict(model=own['model'],tokens=0,input=0,cached=0,
                output=0,reasoning=0,write=0,requests=0,cost=0,unpriced_tokens=0,
                first=own['first'],last=own['last'],efforts={},threads={}))
            for key in ['tokens','input','cached','output','reasoning','write','requests','cost','unpriced_tokens']:
                group[key]+=own[key]
            group['first']=min(group['first'],own['first'])
            group['last']=max(group['last'],own['last'])
            effort=group['efforts'].setdefault(own['effort'],dict(effort=own['effort'],tokens=0,cost=0,requests=0,unpriced_tokens=0))
            contribution=group['threads'].setdefault(thread['id'],dict(id=thread['id'],title=thread['title'],
                tokens=0,cost=0,requests=0,unpriced_tokens=0,last=0))
            for target in [effort,contribution]:
                for key in ['tokens','cost','requests','unpriced_tokens']:target[key]+=own[key]
            contribution['last']=max(contribution['last'],own['last'])
    for group in models.values():
        group['efforts']=sorted(group['efforts'].values(),key=lambda x:-x['tokens'])
        group['threads']=sorted(group['threads'].values(),key=lambda x:-x['tokens'])
    return sorted(models.values(),key=lambda x:-x['tokens'])


class Monitor:
    def __init__(self, home, cache, remote_only=False):
        self.home, self.cache = Path(home), Path(cache)
        self.remote_only = remote_only
        self.lock = threading.RLock()
        self.response_lock = threading.Lock()
        self.response_cache = {}
        self.price_revision = 0
        self.states = {}
        self.speed_evidence = {}
        self._evidence_loaded = False
        self.data = dict(threads=[], indexing=True, progress=0, errors=[], updated=0, cached_threads=0)
        self.stop = threading.Event()
        self.remote = None
        self.receiver_status = 'Remote uploads not configured'
        remote_path = self.cache.parent / '.remote-usage.sqlite'
        if remote_path.exists():
            self.remote = RemoteStore(remote_path)
        self.prices = json.loads((ROOT / 'prices.json').read_text(encoding='utf-8'))
        self.overrides = ROOT / 'price-overrides.json'
        if self.overrides.exists():
            self.prices.update(json.loads(self.overrides.read_text(encoding='utf-8')))
        if not remote_only:
            self.restore_cache()

    def thread_metadata(self):
        dbs = sorted(self.home.glob('state_*.sqlite'), key=lambda p: int(p.stem.split('_')[-1]))
        if not dbs:
            raise RuntimeError('No Codex state database found in ' + str(self.home))
        with closing(readonly(dbs[-1])) as con:
            rows = [dict(r) for r in con.execute('SELECT * FROM threads ORDER BY updated_at DESC')]
            edges = {r['child_thread_id']: dict(r) for r in con.execute('SELECT * FROM thread_spawn_edges')}
        history = {}
        history_path = self.home / 'thread_history_1.sqlite'
        if history_path.exists():
            with closing(readonly(history_path)) as con:
                for r in con.execute('SELECT thread_id,turn_id,status,started_at,completed_at,duration_ms FROM thread_turns'):
                    history.setdefault(r['thread_id'], {})[r['turn_id']] = dict(
                        start=timestamp(r['started_at']), end=timestamp(r['completed_at']) or None,
                        status=r['status'], duration=r['duration_ms'])
        return rows, edges, history

    def restore_cache(self):
        """Publish saved usage before checking logs; never reopen unchanged JSONL files."""
        if not self.cache.exists():
            return
        try:
            with closing(readonly(self.cache)) as con:
                tables = {r['name'] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'rollouts' in tables:
                    for r in con.execute('SELECT id,path,mtime,state FROM rollouts'):
                        try:
                            state = json.loads(r['state'])
                            if state.get('version') == 4:
                                self.states[r['id']] = dict(state=state,key=(r['path'],state['offset'],r['mtime']))
                        except (ValueError, TypeError, KeyError):
                            continue  # Rebuild only this invalid cache entry during scan.
                if 'speed_evidence' in tables:
                    evidence = {}
                    for r in con.execute('SELECT * FROM speed_evidence'):
                        evidence.setdefault((r['thread'],r['turn']),[]).append(json.loads(r['payload']))
                    self.speed_evidence = evidence
                    self._evidence_loaded = True
            if self.states:
                self.rows, self.edges, self.history = self.thread_metadata()
                self.data.update(cached_threads=len(self.states),updated=self.cache.stat().st_mtime)
        except (OSError, sqlite3.Error, RuntimeError, ValueError):
            pass  # Background scan reports unavailable sources and retries.

    def save_prices(self, values):
        clean = {}
        for model, rate in values.items():
            if not isinstance(model, str) or len(model) > 150:
                raise ValueError('Invalid model')
            clean[model] = {}
            for k in ['input', 'cached', 'write', 'output', 'long_threshold']:
                v = float(rate.get(k, 0))
                if not 0 <= v <= 1e9:
                    raise ValueError('Rates must be finite, non-negative numbers')
                clean[model][k] = v
            clean[model].update(source='User override', verified=dt.date.today().isoformat())
        temp = self.overrides.with_suffix('.tmp')
        temp.write_text(json.dumps(clean, indent=2), encoding='utf-8')
        temp.replace(self.overrides)
        with self.lock:
            self.prices = json.loads((ROOT / 'prices.json').read_text(encoding='utf-8')) | clean
            self.price_revision += 1

    def scan(self, cache_db):
        cache_db.execute('CREATE TABLE IF NOT EXISTS speed_evidence (thread TEXT,turn TEXT,time REAL,payload TEXT,PRIMARY KEY(thread,turn,time))')
        cache_db.execute('CREATE TABLE IF NOT EXISTS scan_metadata (key TEXT PRIMARY KEY,value TEXT)')
        logs=self.home/'logs_2.sqlite'
        evidence_changed = False
        if logs.exists():
            try:
                stat = logs.stat()
                identity = [str(logs.resolve()),stat.st_dev,stat.st_ino]
                with closing(readonly(logs)) as con:
                    columns = {r['name'] for r in con.execute('PRAGMA table_info(logs)')}
                    cursor = cache_db.execute("SELECT value FROM scan_metadata WHERE key='diagnostic_cursor'").fetchone()
                    saved = json.loads(cursor[0]) if cursor else {}
                    last_id = 0
                    maximum = None
                    query = "SELECT ts,ts_nanos,thread_id,feedback_log_body FROM logs WHERE target='feedback_tags' AND feedback_log_body LIKE '%service_tier%'"
                    if 'id' in columns:
                        maximum = con.execute('SELECT COALESCE(MAX(id),0) FROM logs').fetchone()[0]
                        if saved.get('identity') == identity and saved.get('last_id',0) <= maximum:
                            last_id = saved.get('last_id',0)
                        query += ' AND id>? AND id<=?'
                    for row in con.execute(query,(last_id,maximum) if maximum is not None else ()):
                        item=diagnostic_tier(row['feedback_log_body'])
                        if item and row['thread_id']:
                            item['time']=row['ts']+(row['ts_nanos'] or 0)/1e9
                            cache_db.execute('INSERT OR REPLACE INTO speed_evidence VALUES (?,?,?,?)',
                                             (row['thread_id'],item['turn_id'],item['time'],json.dumps(item)))
                            evidence_changed = True
                    if maximum is not None and (saved.get('identity') != identity or saved.get('last_id') != maximum):
                        cache_db.execute('INSERT OR REPLACE INTO scan_metadata VALUES (?,?)',
                                         ('diagnostic_cursor',json.dumps(dict(identity=identity,last_id=maximum))))
            except (sqlite3.Error, OSError, ValueError, TypeError):
                pass  # Diagnostic history is optional; token telemetry still works.
        if evidence_changed or not self._evidence_loaded:
            evidence={}
            for thread,turn,_,payload in cache_db.execute('SELECT * FROM speed_evidence'):
                evidence.setdefault((thread,turn),[]).append(json.loads(payload))
            with self.lock:
                self.speed_evidence=evidence
                self._evidence_loaded=True
        rows, edges, history = self.thread_metadata()
        with self.lock:
            self.rows, self.edges, self.history = rows, edges, history
        errors = []
        for i, row in enumerate(rows):
            if self.stop.is_set():
                return
            thread_id = row['id']
            path = Path(row['rollout_path'])
            try:
                stat = path.stat()
                key = (str(path), stat.st_size, stat.st_mtime_ns)
                entry = self.states.get(thread_id)
                if not entry:
                    saved = cache_db.execute('SELECT path,mtime,state FROM rollouts WHERE id=?', (thread_id,)).fetchone()
                    state = json.loads(saved[2]) if saved and saved[0] == str(path) else blank()
                    if state.get('version') != 4:
                        state = blank()
                    if saved and saved[1] != stat.st_mtime_ns and stat.st_size <= state['offset']:
                        state = blank()
                    entry = dict(state=state, key=(saved[0],state['offset'],saved[1]) if saved and state.get('offset') else None)
                if entry['key'] != key:
                    # Same-sized rewrites require a rebuild; appended records are incremental.
                    if entry['key'] and (entry['key'][0] != key[0] or entry['key'][1] == stat.st_size):
                        entry['state'] = blank()
                    entry['state'] = read_increment(path, copy.deepcopy(entry['state']), thread_id)
                    entry['key'] = key
                    cache_db.execute('INSERT OR REPLACE INTO rollouts VALUES (?,?,?,?)',
                                     (thread_id, str(path), stat.st_mtime_ns, json.dumps(entry['state'])))
                with self.lock:
                    self.states[thread_id] = entry
            except (OSError, ValueError) as e:
                errors.append(f"{thread_id}: {type(e).__name__}")
            with self.lock:
                self.data['progress'] = round((i + 1) / len(rows) * 100, 1)
            if i % 50 == 0:
                cache_db.commit()
        cache_db.commit()
        with self.lock:
            self.rows, self.edges, self.history = rows, edges, history
            self.data.update(indexing=False, updated=time.time(), errors=errors)

    def run(self):
        if self.remote_only:
            while not self.stop.is_set():
                with self.lock:
                    self.data.update(indexing=False,progress=100,updated=time.time(),errors=[])
                self.stop.wait(5)
            return
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.cache)) as con:
            con.execute('CREATE TABLE IF NOT EXISTS rollouts (id TEXT PRIMARY KEY,path TEXT,mtime INTEGER,state TEXT)')
            while not self.stop.is_set():
                try:
                    self.scan(con)
                except Exception as e:
                    with self.lock:
                        self.data['errors'] = [str(e)]
                self.stop.wait(5)

    def cached_snapshot(self, device=None, usage_start=0, usage_end=float('inf'), pricing='recorded', cache_policy='recorded', live=False):
        """Coalesce simultaneous browser refreshes; share live results for at most 3 seconds."""
        start_key, end_key = usage_start, usage_end
        if live or usage_end == float('inf'):
            end_key = 'live'
            for span in [86400,604800,2592000]:
                if abs((usage_end-usage_start)-span-1) < 2:
                    start_key = ('rolling',span)
                    break
        key = (device,start_key,end_key,pricing,cache_policy,self.price_revision)
        with self.response_lock:
            now = time.monotonic()
            saved = self.response_cache.get(key)
            if saved and now-saved[0] < 3:
                return saved[1]
            result = self.snapshot(device,usage_start,usage_end,pricing,cache_policy)
            now = time.monotonic()
            self.response_cache = {k:v for k,v in self.response_cache.items() if now-v[0] < 3}
            if len(self.response_cache) >= 4:
                self.response_cache.pop(min(self.response_cache,key=lambda k:self.response_cache[k][0]))
            self.response_cache[key] = (now,result)
            return result

    def snapshot(self, device=None, usage_start=0, usage_end=float('inf'), pricing='recorded', cache_policy='recorded'):
        with self.lock:
            result = copy.deepcopy(self.data)
            prices = copy.deepcopy(self.prices)
            if pricing == 'fast':
                # Apply only to unknown tiers. Explicit recorded tiers always win.
                prices = copy.deepcopy(prices)
            rows, states, edges, history, devices = self.collection(device)
        states=cache_pricing_states(rows,states,prices,cache_policy)
        now = time.time()
        windows = {}
        for entry in states.values():
            for w in entry['state']['limits']:
                key = (w['id'], w.get('window_minutes'))
                if w['time'] > windows.get(key, {}).get('time', 0):
                    windows[key] = w
        result['limits'] = sorted(windows.values(), key=lambda x: (x['id'], x.get('window_minutes', 0)))
        weekly = next((w for w in result['limits'] if w['id'] == 'codex' and w.get('window_minutes') == 10080), None)
        if weekly and weekly.get('resets_at', 0) <= now:
            weekly = None
        result['weekly'] = weekly
        start = weekly['resets_at'] - 604800 if weekly else now - 604800
        end = weekly['time'] if weekly else now
        items = []
        days = {}
        all_tokens = all_cost = all_unpriced = unknown_tier_tokens = 0
        speed_counts={k:0 for k in ['standard','fast','other','absent','null','empty']}
        speed_sources={'session':0,'diagnostic_selected':0}
        cache_breaks=cache_adjusted_tokens=cache_parent_breaks=0
        for row in rows:
            s = states.get(row['id'], {}).get('state', blank())
            groups = {}
            week_tokens = 0
            for record in s['records']:
                actual = record
                if pricing == 'fast' and record.get('tier','unknown') in ['unknown','auto',None]:
                    actual = dict(record,tier='fast')
                cost = price_record(actual, prices)
                u = record['usage']
                all_tokens += u['total_tokens']; all_cost += cost or 0
                all_unpriced += u['total_tokens'] if cost is None else 0
                if start <= record['time'] <= end:
                    week_tokens += u['total_tokens']
                if not usage_start <= record['time'] < usage_end:
                    continue
                speed_counts[speed_category(record)]+=1
                speed_sources['diagnostic_selected' if record.get('tier_origin')=='diagnostic_selected' else 'session']+=1
                cache_breaks+=int(bool(record.get('cache_break')))
                cache_parent_breaks+=int(record.get('cache_break_from_parent',False))
                cache_adjusted_tokens+=record.get('cache_adjusted_tokens',0)
                unknown_tier_tokens += u['total_tokens'] if record.get('tier','unknown') in ['unknown','auto',None] else 0
                day = int(record['time']//86400)*86400
                bucket = days.setdefault(day,dict(time=day,tokens=0,cost=0,unpriced_tokens=0,models={},model_costs={},model_unpriced={}))
                bucket['tokens'] += u['total_tokens']; bucket['cost'] += cost or 0
                bucket['unpriced_tokens'] += u['total_tokens'] if cost is None else 0
                bucket['models'][record['model']] = bucket['models'].get(record['model'],0)+u['total_tokens']
                bucket['model_costs'][record['model']] = bucket['model_costs'].get(record['model'],0)+(cost or 0)
                bucket['model_unpriced'][record['model']] = bucket['model_unpriced'].get(record['model'],0)+(u['total_tokens'] if cost is None else 0)
                key = (record['model'], record['effort'],record.get('tier','unknown'))
                g = groups.setdefault(key, dict(model=key[0], effort=key[1], requests=0,
                    tier=key[2],
                    tokens=0, input=0, cached=0, output=0, reasoning=0, write=0, cost=0,
                    unpriced_tokens=0, long_requests=0, first=record['time'], last=record['time']))
                u = record['usage']
                g['requests'] += 1
                g['last'] = record['time']
                g['tokens'] += u['total_tokens']
                for short, full in [('input','input_tokens'), ('cached','cached_input_tokens'),
                                    ('output','output_tokens'), ('reasoning','reasoning_output_tokens'),
                                    ('write','cache_write_input_tokens')]:
                    g[short] += u[full]
                if cost is None:
                    g['unpriced_tokens'] += u['total_tokens']
                else:
                    g['cost'] += cost
                    threshold = (resolved_rate(record,prices) or {}).get('long_threshold', 0)
                    g['long_requests'] += int(bool(threshold and u['input_tokens'] > threshold))
            turns = s['turns'] | history.get(row['id'], {})
            latest = max(turns.values(), key=lambda t: t['start'], default=None)
            status = latest['status'] if latest else 'unknown'
            running = status == 'inProgress'
            # Old interrupted processes sometimes leave inProgress in the projection.
            stale = running and now - max(s['latest'], row['updated_at']) > 900
            observed_now = min(now, row['_sync_time']) if row.get('_sync_time') and now-row['_sync_time']>90 else now
            runtime = max(0, observed_now - latest['start']) if running and latest else 0
            duration = sum(max(0, t.get('duration') / 1000 if t.get('duration') is not None else
                               (t['end'] or (observed_now if t is latest and not stale else t['start'])) - t['start'])
                           for t in turns.values() if t['start'])
            period_duration = sum(max(0,min(t['end'] or (t['start'] if stale else observed_now),usage_end)-max(t['start'],usage_start))
                                  for t in turns.values() if t['start']) if usage_start or usage_end<now else duration
            parent = edges.get(row['id'], {}).get('parent_thread_id') or s['parent']
            source = row.get('source') or ''
            if not parent and source.startswith('{'):
                try:
                    parent = json.loads(source).get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id')
                except (ValueError, AttributeError):
                    pass
            item = dict(id=row['id'], title=row.get('name') or row['title'],
                cwd=row['cwd'].removeprefix('\\\\?\\'), archived=bool(row['archived']),
                model=s['model'] if s['model'] != 'unknown' else row.get('model'),
                effort=s['effort'] if s['effort'] != 'unknown' else row.get('reasoning_effort'),
                parent=parent, agent=bool(parent or 'subagent' in source),
                role=row.get('agent_role'), nickname=row.get('agent_nickname'),
                agent_path=row.get('agent_path'), status='unconfirmed' if stale else status,
                active=running and not stale, runtime=runtime, duration=duration,
                created=row['created_at'], updated=row['updated_at'], groups=list(groups.values()),
                contexts=s['contexts'], tokens=sum(g['tokens'] for g in groups.values()),
                database_tokens=row['tokens_used'], cost=sum(g['cost'] for g in groups.values()),
                unpriced_tokens=sum(g['unpriced_tokens'] for g in groups.values()),
                week_tokens=week_tokens, indexed=bool(states.get(row['id'])),
                malformed=s['malformed'], turns=len(turns), device=row.get('_device','local'),
                device_name=row.get('_device_name',socket.gethostname()),
                sync_time=row.get('_sync_time'),
                sync_stale=bool(row.get('_sync_time') and now-row['_sync_time']>90))
            item['duration'] = period_duration
            item['period_activity'] = bool(groups or period_duration)
            item['usage_last'] = max((g['last'] for g in groups.values()),default=0)
            if (usage_start or usage_end<now) and groups:
                last_group=max(groups.values(),key=lambda g:g['last'])
                item['model'],item['effort']=last_group['model'],last_group['effort']
            if item['sync_stale']:
                item['active'] = False
                if item['status'] == 'inProgress':
                    item['status'] = 'unconfirmed'
            items.append(item)
        by_id = {item['id']: item for item in items}
        for item in items:
            item['children'] = []
        for item in items:
            if item['parent'] in by_id:
                by_id[item['parent']]['children'].append(item['id'])
        def family(item, seen):
            if item['id'] in seen:
                return []
            seen.add(item['id'])
            return [item] + [d for child in item['children'] for d in family(by_id[child], seen)]
        total_week = sum(i['week_tokens'] for i in items)
        for item in items:
            descendants = family(item, set())
            item['family_tokens'] = sum(d['tokens'] for d in descendants)
            item['family_cost'] = sum(d['cost'] for d in descendants)
            item['family_unpriced_tokens'] = sum(d['unpriced_tokens'] for d in descendants)
            item['agent_count'] = len(descendants) - 1
            item['family_active'] = any(d['active'] for d in descendants)
            item['family_week_tokens'] = sum(d['week_tokens'] for d in descendants)
            item['family_period_activity'] = any(d['period_activity'] for d in descendants)
            item['family_usage_last'] = max(d['usage_last'] for d in descendants)
            item['weekly_estimate'] = (weekly['used_percent'] * item['family_week_tokens'] / total_week
                                       if weekly and total_week else None)
        items.sort(key=lambda i: (not i['family_active'], -i['updated']))
        result.update(threads=items, prices=prices, now=now, home='Remote collectors' if self.remote_only else str(self.home),
            total_tokens=sum(i['tokens'] for i in items), total_cost=sum(i['cost'] for i in items),
            unpriced_tokens=sum(i['unpriced_tokens'] for i in items),
            active=sum(i['active'] for i in items), week_tokens=total_week,
            devices=devices, receiver_status=self.receiver_status)
        result.update(start=usage_start,end=None if usage_end==float('inf') else usage_end,
                      pricing=pricing,unknown_tier_tokens=unknown_tier_tokens,
                      all_time=dict(tokens=all_tokens,cost=all_cost,unpriced_tokens=all_unpriced),
                      daily=sorted(days.values(),key=lambda d:d['time']))
        result.update(speed_counts=speed_counts,speed_sources=speed_sources,cache_policy=cache_policy,cache_breaks=cache_breaks,
                      cache_parent_breaks=cache_parent_breaks,cache_adjusted_tokens=cache_adjusted_tokens)
        result['models']=models_from_threads(items)
        return result

    def collection(self, device=None):
        """Choose one complete snapshot per thread ID across devices."""
        candidates = {}
        states, edges, history = {}, {}, {}
        devices = [] if self.remote_only else [dict(id='local',name=socket.gethostname()+' (this PC)',seen=self.data['updated'])]
        def add(row,state,owner,name,seen,turns):
            if device and owner != device:
                return
            key=row['id']
            rank=(max((r['time'] for r in state.get('records',[])),default=0),
                  len(state.get('records',[])),row.get('updated_at',0))
            if key in candidates and rank <= candidates[key][0]:
                return
            value=dict(row,_device=owner,_device_name=name,_sync_time=seen)
            candidates[key]=(rank,value)
            states[key]=dict(state=state)
            parent=state.get('parent')
            if parent:edges[key]=dict(parent_thread_id=parent)
            else:edges.pop(key,None)
            history[key]=turns
        for row in getattr(self,'rows',[]):
            state=recover_tiers(self.states.get(row['id'],{}).get('state',blank()),row['id'],self.speed_evidence)
            parent=getattr(self,'edges',{}).get(row['id'],{}).get('parent_thread_id')
            if parent:state=dict(state,parent=parent)
            add(row,state,'local',socket.gethostname(),None,getattr(self,'history',{}).get(row['id'],{}))
        if self.remote:
            remote_devices, remote_threads=self.remote.snapshot_refs()
            devices.extend(remote_devices.values())
            for (owner,key),item in remote_threads.items():
                dev=remote_devices[owner]
                add(item['row'],item['state'],owner,dev['name'],dev['seen'],{})
        rows=[v[1] for v in candidates.values() if not device or v[1]['_device']==device]
        selected={r['id'] for r in rows}
        return rows,{k:v for k,v in states.items() if k in selected},edges,history,devices

    def export_for_sync(self):
        # Never send database columns containing message bodies or auth files.
        with self.lock:
            exported=[]
            for row in getattr(self,'rows',[]):
                state=recover_tiers(self.states.get(row['id'],{}).get('state',blank()),row['id'],self.speed_evidence)
                state=dict(state,turns=state['turns'] | getattr(self,'history',{}).get(row['id'],{}),
                           parent=getattr(self,'edges',{}).get(row['id'],{}).get('parent_thread_id') or state.get('parent'))
                exported.append(clean_thread(dict(row=row,state=state)))
            return exported

    def model_snapshot(self, start=0, end=float('inf'), device=None, pricing='recorded', cache_policy='recorded'):
        with self.lock:
            rows,states,_,_,_=self.collection(device)
            prices=copy.deepcopy(self.prices)
            updated=self.data['updated']
        states=cache_pricing_states(rows,states,prices,cache_policy)
        if pricing=='fast':
            for entry in states.values():
                for record in entry['state']['records']:
                    if record.get('tier','unknown') in ['unknown','auto',None]:record['tier']='fast'
        return dict(models=summarize_models(rows,states,prices,start,end),updated=updated,start=start,
                    end=end if end!=float('inf') else None)


def serve(monitor, port):
    allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    access_config = ROOT / 'dashboard-access.json'
    if access_config.exists():
        allowed_hosts.update(json.loads(access_config.read_text(encoding='utf-8'))['hosts'])
    allowed_hosts = {host.lower() for host in allowed_hosts}
    allowed_origins = {'http://' + host for host in allowed_hosts}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def allowed(self):
            return self.headers.get('Host', '').lower() in allowed_hosts

        def send(self, status, data, mime='application/json'):
            body=data if isinstance(data,bytes) else json.dumps(data,separators=(',',':')).encode()
            compressed=mime=='application/json' and len(body)>4096 and 'gzip' in self.headers.get('Accept-Encoding','')
            if compressed:body=gzip.compress(body,compresslevel=1)
            self.send_response(status)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.send_header('Content-Length',str(len(body)))
            if compressed:
                self.send_header('Content-Encoding','gzip')
                self.send_header('Vary','Accept-Encoding')
            self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass

        def do_GET(self):
            if not self.allowed():
                return self.send(403, {'error': 'Dashboard host not allowed'})
            path = urlparse(self.path).path
            if path in ['/api/data','/api/models']:
                try:
                    query = parse_qs(urlparse(self.path).query)
                    start = float(query.get('start', ['0'])[0])
                    end = float(query.get('end', [str(time.time() + 1)])[0])
                    if not 0 <= start < end < 1e12:
                        raise ValueError('Invalid date range')
                    pricing=query.get('pricing',['recorded'])[0]
                    if pricing not in ['recorded','fast']: raise ValueError('Invalid pricing scenario')
                    cache_policy=query.get('cache',['recorded'])[0]
                    if cache_policy not in ['recorded','switches']:raise ValueError('Invalid cache policy')
                    if path=='/api/data':
                        live = 'end' not in query or query.get('live',['0'])[0] == '1'
                        return self.send(200, monitor.cached_snapshot(query.get('device',[None])[0],start,end,pricing,cache_policy,live=live))
                    return self.send(200, monitor.model_snapshot(start, end,query.get('device',[None])[0],pricing,cache_policy))
                except ValueError as e:
                    return self.send(400, {'error': str(e)})
            files = {'/': ('index.html','text/html'), '/app.js': ('app.js','text/javascript'),
                     '/style.css': ('style.css','text/css')}
            if path in files:
                name, mime = files[path]
                return self.send(200, (ROOT / 'web' / name).read_bytes(), mime)
            self.send(404, {'error': 'Not found'})

        def do_POST(self):
            if not self.allowed() or self.headers.get('Origin') not in allowed_origins or self.headers.get('Origin') != 'http://' + self.headers.get('Host', '').lower():
                return self.send(403, {'error': 'Same-origin access required'})
            if self.path != '/api/prices':
                return self.send(404, {'error': 'Not found'})
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size < 100000:
                    raise ValueError('Invalid request size')
                monitor.save_prices(json.loads(self.rfile.read(size)))
                self.send(200, {'ok': True})
            except (ValueError, TypeError, AttributeError) as e:
                self.send(400, {'error': str(e)})
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    receiver = None
    remote_config = ROOT / 'remote-config.json'
    if remote_config.exists():
        try:
            config = json.loads(remote_config.read_text(encoding='utf-8'))
            if not monitor.remote:
                monitor.remote = RemoteStore(monitor.cache.parent / '.remote-usage.sqlite')
            receiver = make_receiver(monitor.remote,remote_config,config['bind'],config['port'])
            threading.Thread(target=receiver.serve_forever,daemon=True).start()
            endpoint=config.get('endpoint') or f"http://{config['bind']}:{config['port']}"
            monitor.receiver_status = f"Uploads at {endpoint}" + (' via Tailscale Serve' if config.get('transport')=='tailscale-serve' else '')
        except (OSError,ValueError,KeyError) as e:
            monitor.receiver_status = 'Remote receiver unavailable: '+str(e)
    threading.Thread(target=monitor.run, daemon=True).start()
    print(f'Codex Monitor: http://127.0.0.1:{port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop.set()
        if receiver:
            receiver.shutdown()
            receiver.server_close()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', default=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--remote-only', action='store_true', help='Serve uploaded history without scanning this host for local Codex logs')
    args = parser.parse_args()
    serve(Monitor(args.home, ROOT / '.monitor-cache.sqlite',remote_only=args.remote_only), args.port)
