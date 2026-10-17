import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import monitor as implementation
from monitor import Monitor


class StartupCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.cache_path = self.home / 'cache.sqlite'
        self.path = self.home / 'chat.jsonl'
        self.source = sqlite3.connect(self.home / 'state_1.sqlite')
        self.source.execute('CREATE TABLE threads (id TEXT,title TEXT,cwd TEXT,rollout_path TEXT,created_at INTEGER,updated_at INTEGER,tokens_used INTEGER,archived INTEGER,source TEXT)')
        self.source.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT,child_thread_id TEXT)')
        self.source.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)',
                            ('chat','Cached chat',str(self.home),str(self.path),1,1,110,0,'cli'))
        self.source.commit()
        self.cache = sqlite3.connect(self.cache_path)
        self.cache.execute('CREATE TABLE rollouts (id TEXT PRIMARY KEY,path TEXT,mtime INTEGER,state TEXT)')
        self.cache.commit()
        self.write_usage(100,'first',replace=True)

    def tearDown(self):
        self.source.close()
        self.cache.close()
        self.temp.cleanup()

    def write_usage(self, amount, identifier, replace=False, path=None, complete=True):
        event = dict(type='token_usage_record',timestamp='2026-10-05T00:00:00Z',
                     payload=dict(thread_id='chat',response_id=identifier,model='gpt-6-sol',
                                  usage=dict(input_tokens=amount,output_tokens=10,total_tokens=amount+10)))
        value = json.dumps(event) + ('\n' if complete else '')
        with (path or self.path).open('w' if replace else 'a',encoding='utf-8') as stream:
            stream.write(value)

    def initial_scan(self):
        value = Monitor(self.home,self.cache_path)
        value.scan(self.cache)
        return value

    def test_restart_publishes_saved_usage_and_skips_unchanged_files_and_writes(self):
        self.initial_scan()
        restarted = Monitor(self.home,self.cache_path)
        self.assertEqual(restarted.snapshot()['total_tokens'],110)
        self.assertEqual(restarted.data['cached_threads'],1)
        writes = self.cache.total_changes
        with patch.object(implementation,'read_increment',side_effect=AssertionError('Unchanged log opened')):
            restarted.scan(self.cache)
            restarted.scan(self.cache)
        self.assertEqual(self.cache.total_changes,writes)
        self.assertFalse(restarted.data['indexing'])

    def test_restart_reads_only_appended_bytes_and_keeps_prior_state_immutable(self):
        self.initial_scan()
        restarted = Monitor(self.home,self.cache_path)
        prior = restarted.states['chat']['state']
        saved = copy.deepcopy(prior)
        self.write_usage(200,'second')
        with patch.object(implementation,'read_increment',wraps=implementation.read_increment) as read:
            restarted.scan(self.cache)
        self.assertEqual(read.call_count,1)
        self.assertEqual(read.call_args.args[1]['records'][0]['usage']['input_tokens'],100)
        self.assertEqual(prior,saved)
        self.assertEqual(restarted.snapshot()['total_tokens'],320)

    def test_same_size_rewrite_truncation_and_relocated_logs_rebuild_individually(self):
        value = self.initial_scan()
        previous = self.path.stat()
        self.write_usage(200,'first',replace=True)
        self.assertEqual(self.path.stat().st_size,previous.st_size)
        os.utime(self.path,ns=(previous.st_atime_ns,previous.st_mtime_ns+10000000))
        value.scan(self.cache)
        self.assertEqual(value.snapshot()['total_tokens'],210)
        moved = self.home / 'moved.jsonl'
        self.write_usage(300,'first',replace=True,path=moved)
        self.source.execute('UPDATE threads SET rollout_path=?',(str(moved),))
        self.source.commit()
        value.scan(self.cache)
        self.assertEqual(value.snapshot()['total_tokens'],310)
        moved.write_bytes(b'')
        value.scan(self.cache)
        self.assertEqual(value.states['chat']['state']['records'],[])

    def test_restart_partial_tail_is_retried_without_duplicate_usage(self):
        self.initial_scan()
        self.write_usage(200,'second',complete=False)
        restarted = Monitor(self.home,self.cache_path)
        restarted.scan(self.cache)
        self.assertEqual(restarted.snapshot()['total_tokens'],110)
        restarted = Monitor(self.home,self.cache_path)
        with self.path.open('ab') as stream:
            stream.write(b'\n')
        restarted.scan(self.cache)
        self.assertEqual(restarted.snapshot()['total_tokens'],320)

    def test_response_cache_separates_filters_expiration_and_pricing_updates(self):
        value = self.initial_scan()
        with patch.object(value,'snapshot',side_effect=lambda *args: {'args':args}) as compute,patch.object(implementation.time,'monotonic',return_value=100) as clock:
            a = value.cached_snapshot(usage_end=1001,live=True)
            self.assertIs(value.cached_snapshot(usage_end=1002,live=True),a)
            self.assertEqual(compute.call_count,1)
            value.cached_snapshot(device='laptop',usage_end=1001,live=True)
            value.cached_snapshot(usage_end=1001,cache_policy='switches',live=True)
            value.cached_snapshot(usage_end=1001,pricing='fast',live=True)
            self.assertEqual(compute.call_count,4)
            # Fixed/custom endpoints must never collide with a live query.
            value.cached_snapshot(usage_end=1001)
            value.cached_snapshot(usage_end=1002)
            self.assertEqual(compute.call_count,6)
            self.assertLessEqual(len(value.response_cache),4)
            clock.return_value=104
            value.cached_snapshot(usage_end=1003,live=True)
            self.assertEqual(compute.call_count,7)
            value.price_revision+=1
            value.cached_snapshot(usage_end=1003,live=True)
            self.assertEqual(compute.call_count,8)

    def test_concurrent_live_responses_compute_once(self):
        value=self.initial_scan()
        entered=threading.Event();release=threading.Event();results=[]
        def compute(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return {'tokens':110}
        with patch.object(value,'snapshot',side_effect=compute) as calculate:
            a=threading.Thread(target=lambda:results.append(value.cached_snapshot(usage_end=1001,live=True)))
            b=threading.Thread(target=lambda:results.append(value.cached_snapshot(usage_end=1002,live=True)))
            a.start();self.assertTrue(entered.wait(2));b.start();release.set();a.join(2);b.join(2)
            self.assertEqual(calculate.call_count,1)
        self.assertEqual(len(results),2)
        self.assertIs(results[0],results[1])

    def test_diagnostic_checkpoint_survives_restart_and_database_reset(self):
        logs_path = self.home / 'logs_2.sqlite'
        def insert(con,turn,when):
            body = 'turn_id='+turn+' model=gpt-6-sol tags_json={"service_tier":"priority"}'
            con.execute('INSERT INTO logs (ts,ts_nanos,thread_id,target,feedback_log_body) VALUES (?,0,?,?,?)',
                        (when,'chat','feedback_tags',body))
            con.commit()
        with closing(sqlite3.connect(logs_path)) as logs:
            logs.execute('CREATE TABLE logs (id INTEGER PRIMARY KEY,ts INTEGER,ts_nanos INTEGER,thread_id TEXT,target TEXT,feedback_log_body TEXT)')
            insert(logs,'one',10)
            self.initial_scan()
            restarted = Monitor(self.home,self.cache_path)
            self.assertIn(('chat','one'),restarted.speed_evidence)
            with patch.object(implementation,'diagnostic_tier',side_effect=AssertionError('Old diagnostic rescanned')):
                restarted.scan(self.cache)
            insert(logs,'two',20)
            with patch.object(implementation,'diagnostic_tier',wraps=implementation.diagnostic_tier) as parse:
                restarted.scan(self.cache)
            self.assertEqual(parse.call_count,1)
            self.assertIn(('chat','two'),restarted.speed_evidence)
            logs.execute('DELETE FROM logs')
            insert(logs,'three',30)  # IDs restart below the saved cursor.
            restarted.scan(self.cache)
            self.assertIn(('chat','three'),restarted.speed_evidence)
            self.assertIn(('chat','one'),restarted.speed_evidence)
        rotated = self.home / 'rotated.sqlite'
        with closing(sqlite3.connect(rotated)) as logs:
            logs.execute('CREATE TABLE logs (id INTEGER PRIMARY KEY,ts INTEGER,ts_nanos INTEGER,thread_id TEXT,target TEXT,feedback_log_body TEXT)')
            insert(logs,'four',40)  # Same maximum ID, but a different database identity.
        rotated.replace(logs_path)
        restarted.scan(self.cache)
        self.assertIn(('chat','four'),restarted.speed_evidence)
        self.assertIn(('chat','one'),restarted.speed_evidence)


if __name__ == '__main__':
    unittest.main()
