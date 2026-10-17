import json
from pathlib import Path
import tempfile
import unittest
import sqlite3
from monitor import blank, consume, price_record, read_increment, Monitor, summarize_models, cache_break_reason, cache_pricing_states


def event(kind, payload, ordinal=20, time='2026-10-02T05:00:00Z'):
    return dict(type=kind, payload=payload, ordinal=ordinal, timestamp=time)


def tokens(input=100, cached=40, output=10, reasoning=3, write=0):
    return dict(input_tokens=input, cached_input_tokens=cached, output_tokens=output,
                reasoning_output_tokens=reasoning, cache_write_input_tokens=write,
                total_tokens=input+output)


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.state = blank()
        consume(self.state, event('turn_context', dict(model='first', effort='high')), 'child')

    def test_modern_and_counter_are_counted_once(self):
        value = tokens()
        raw = event('token_usage_record', dict(thread_id='child', response_id='one', usage=value,
                                              thread_token_usage=value))
        consume(self.state, raw, 'child')
        consume(self.state, raw, 'child')
        consume(self.state, event('event_msg', dict(type='token_count', info=dict(
            total_token_usage=value, last_token_usage=value))), 'child')
        self.assertEqual(len(self.state['records']), 1)
        self.assertEqual(self.state['records'][0]['usage']['total_tokens'], 110)

    def test_model_switch_preserves_old_usage(self):
        consume(self.state, event('event_msg', dict(type='token_count', info=dict(total_token_usage=tokens()))), 'child')
        consume(self.state, event('turn_context', dict(model='second', effort='low')), 'child')
        consume(self.state, event('event_msg', dict(type='token_count', info=dict(total_token_usage=tokens(300,80,20)))), 'child')
        self.assertEqual([r['model'] for r in self.state['records']], ['first','second'])
        self.assertEqual([r['usage']['total_tokens'] for r in self.state['records']], [110,210])
        self.assertEqual(self.state['records'][1]['effort'], 'low')

    def test_migration_request_counter_restarts_but_legacy_counter_continues(self):
        consume(self.state, event('event_msg',dict(type='token_count',info=dict(total_token_usage=tokens(10000,4000,1000)))), 'child')
        consume(self.state, event('token_usage_record',dict(thread_id='child',response_id='new',usage=tokens(),thread_token_usage=tokens())), 'child')
        consume(self.state, event('event_msg',dict(type='token_count',info=dict(total_token_usage=tokens(10100,4040,1010),last_token_usage=tokens()))), 'child')
        self.assertEqual(sum(r['usage']['total_tokens'] for r in self.state['records']),11110)
        self.assertEqual(len(self.state['records']),2)

    def test_inherited_records_and_turns_excluded(self):
        consume(self.state, event('session_meta', dict(parent_thread_id='parent', subagent_history_start_ordinal=10)), 'child')
        consume(self.state, event('token_usage_record', dict(thread_id='parent', usage=tokens())), 'child')
        consume(self.state, event('event_msg', dict(type='task_started',turn_id='copied'), ordinal=4), 'child')
        consume(self.state, event('event_msg', dict(type='token_count',info=dict(total_token_usage=tokens())), ordinal=5), 'child')
        consume(self.state, event('event_msg', dict(type='token_count',info=dict(total_token_usage=tokens(200,80,20))), ordinal=15), 'child')
        self.assertEqual(len(self.state['records']), 1)
        self.assertEqual(self.state['records'][0]['usage']['total_tokens'], 110)
        self.assertEqual(self.state['turns'], {})

    def test_counter_reset_uses_last_request(self):
        consume(self.state, event('event_msg', dict(type='token_count',info=dict(total_token_usage=tokens(900,400,100)))), 'child')
        consume(self.state, event('event_msg', dict(type='token_count',info=dict(total_token_usage=tokens(100,0,10),last_token_usage=tokens(50,0,5)))), 'child')
        self.assertEqual(self.state['records'][-1]['usage']['total_tokens'], 55)

    def test_cached_reasoning_and_long_context_pricing(self):
        rates={'first':dict(input=2,cached=.1,write=2.5,output=10,long_threshold=272000)}
        record=dict(model='first',usage=tokens(100,40,10,3,10))
        self.assertAlmostEqual(price_record(record,rates), (50*2+40*.1+10*2.5+10*10)/1e6)
        record['usage']=tokens(300000,100000,100,20,10000)
        self.assertAlmostEqual(price_record(record,rates),(2*(190000*2+100000*.1+10000*2.5)+1.5*100*10)/1e6)
        self.assertIsNone(price_record(record,{}))

    def test_partial_append_then_complete_and_truncate(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'log.jsonl'
            line=json.dumps(event('event_msg',dict(type='token_count',info=dict(total_token_usage=tokens())))).encode()
            path.write_bytes(line[:80])
            state=read_increment(path,blank(),'child')
            self.assertEqual(state['offset'],0)
            with path.open('ab') as stream: stream.write(line[80:]+b'\n')
            state=read_increment(path,state,'child')
            self.assertEqual(len(state['records']),1)
            state=read_increment(path,state,'child')
            self.assertEqual(len(state['records']),1)
            path.write_bytes(b'')
            state=read_increment(path,state,'child')
            self.assertEqual(state['records'],[])

    def test_family_rollup_and_account_total_are_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor=Monitor(folder,Path(folder)/'cache')
            monitor.rows=[dict(id=i,title=i,cwd=folder,archived=0,created_at=1,updated_at=1,tokens_used=110)
                          for i in ['parent','child','grandchild']]
            monitor.edges={'child':dict(parent_thread_id='parent'),'grandchild':dict(parent_thread_id='child')}
            monitor.history={}
            for i in ['parent','child','grandchild']:
                state=blank();consume(state,event('token_usage_record',dict(thread_id=i,usage=tokens())),i)
                monitor.states[i]=dict(state=state)
            snap=monitor.snapshot();by_id={t['id']:t for t in snap['threads']}
            self.assertEqual(snap['total_tokens'],330)
            self.assertEqual(by_id['parent']['family_tokens'],330)
            self.assertEqual(by_id['child']['family_tokens'],220)
            self.assertEqual(by_id['parent']['agent_count'],2)

    def test_custom_rates_persist_and_reject_invalid_values(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor=Monitor(folder,Path(folder)/'cache')
            monitor.overrides=Path(folder)/'overrides.json'
            monitor.save_prices({'custom':dict(input=1,cached=.1,write=1,output=2,long_threshold=0)})
            self.assertEqual(monitor.prices['custom']['output'],2)
            self.assertEqual(json.loads(monitor.overrides.read_text())['custom']['source'],'User override')
            with self.assertRaises(ValueError):
                monitor.save_prices({'custom':dict(input=float('nan'))})
            self.assertEqual(monitor.prices['custom']['input'],1)
            monitor.save_prices({})
            self.assertNotIn('custom',monitor.prices)

    def test_model_totals_dates_efforts_and_mid_chat_switch(self):
        state=blank()
        for model,effort,time in [('first','high',100),('second','low',200),('first','low',300)]:
            state['records'].append(dict(model=model,effort=effort,time=time,usage=tokens()))
        rows=[dict(id='chat',title='Chat')]
        states={'chat':dict(state=state)}
        all_time=summarize_models(rows,states,{})
        self.assertEqual(sum(m['tokens'] for m in all_time),330)
        first=next(m for m in all_time if m['model']=='first')
        self.assertEqual(first['tokens'],220)
        self.assertEqual({e['effort']:e['tokens'] for e in first['efforts']},{'high':110,'low':110})
        self.assertEqual(len(first['threads']),1)
        self.assertEqual(first['unpriced_tokens'],220)
        selected=summarize_models(rows,states,{},200,300)
        self.assertEqual([(m['model'],m['tokens']) for m in selected],[('second',110)])

    def test_scan_discovers_new_chats_and_appended_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)
            source=sqlite3.connect(home/'state_1.sqlite')
            source.execute('CREATE TABLE threads (id TEXT, title TEXT, rollout_path TEXT, updated_at INTEGER)')
            source.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT,child_thread_id TEXT,status TEXT)')
            cache=sqlite3.connect(home/'cache.sqlite')
            cache.execute('CREATE TABLE rollouts (id TEXT PRIMARY KEY,path TEXT,mtime INTEGER,state TEXT)')
            monitor=Monitor(home,home/'cache.sqlite')
            path=home/'chat.jsonl'
            def append(path,events):
                with path.open('a',encoding='utf-8') as stream:
                    for e in events:stream.write(json.dumps(e)+'\n')
            append(path,[event('turn_context',dict(model='first',effort='high')),
                         event('event_msg',dict(type='token_count',info=dict(total_token_usage=tokens())))])
            source.execute('INSERT INTO threads VALUES (?,?,?,?)',('chat','Chat',str(path),1));source.commit()
            monitor.scan(cache)
            self.assertEqual(monitor.model_snapshot()['models'][0]['tokens'],110)
            append(path,[event('turn_context',dict(model='second',effort='low')),
                         event('event_msg',dict(type='token_count',info=dict(total_token_usage=tokens(200,80,20))))])
            second=home/'new.jsonl'
            append(second,[event('turn_context',dict(model='second',effort='low')),
                           event('event_msg',dict(type='token_count',info=dict(total_token_usage=tokens())))])
            source.execute('INSERT INTO threads VALUES (?,?,?,?)',('new','New chat',str(second),2));source.commit()
            monitor.scan(cache)
            summary={m['model']:m for m in monitor.model_snapshot()['models']}
            self.assertEqual(summary['first']['tokens'],110)
            self.assertEqual(summary['second']['tokens'],220)
            self.assertEqual(len(summary['second']['threads']),2)
            source.close();cache.close()

    def test_period_graphs_headlines_and_family_agree(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor=Monitor(folder,Path(folder)/'cache')
            monitor.rows=[dict(id=i,title=i,cwd=folder,archived=0,created_at=1,updated_at=1,tokens_used=0)
                          for i in ['parent','child']]
            monitor.edges={'child':dict(parent_thread_id='parent')};monitor.history={}
            for i in ['parent','child']:
                state=blank()
                state['records']=[dict(time=t,model='gpt-6-sol',effort='high',usage=tokens()) for t in [100,200,300]]
                state['turns']={'turn':dict(start=90,end=310,status='completed')}
                monitor.states[i]=dict(state=state)
            snap=monitor.snapshot(usage_start=200,usage_end=300)
            self.assertEqual(snap['total_tokens'],220)
            self.assertEqual(snap['all_time']['tokens'],660)
            self.assertEqual(sum(d['tokens'] for d in snap['daily']),220)
            self.assertAlmostEqual(sum(d['cost'] for d in snap['daily']),snap['total_cost'])
            self.assertEqual(next(t for t in snap['threads'] if t['id']=='parent')['family_tokens'],220)
            self.assertEqual(snap['threads'][0]['duration'],100)
            models=monitor.model_snapshot(200,300)['models']
            self.assertEqual(snap['models'],models)
            self.assertEqual(sum(m['tokens'] for m in models),snap['total_tokens'])
            fast=monitor.snapshot(usage_start=200,usage_end=300,pricing='fast')
            self.assertEqual(fast['models'],monitor.model_snapshot(200,300,pricing='fast')['models'])
            self.assertAlmostEqual(fast['total_cost'],snap['total_cost']*2)

    def test_daybreak_historical_sol_fallback_and_fast_tier(self):
        with tempfile.TemporaryDirectory() as folder:
            rates=Monitor(folder,Path(folder)/'cache').prices
            cutover=rates['gpt-daybreak-blue-latest']['history'][1]['from']
            for t,model in [(cutover-1,'gpt-5.6-sol'),(cutover,'gpt-6-sol'),(cutover+30*86400,'gpt-6-sol')]:
                record=dict(time=t,model='gpt-daybreak-blue-latest',usage=tokens())
                self.assertEqual(price_record(record,rates),price_record(dict(record,model=model),rates))
                self.assertAlmostEqual(price_record(dict(record,tier='fast'),rates),price_record(record,rates)*2)
                self.assertIsNone(price_record(dict(record,tier='unsupported'),rates))

    def test_reserve_dated_luna_prices_tiers_and_cache_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            rates=Monitor(folder,Path(folder)/'cache').prices
            cutover=rates['gpt-reserve']['history'][1]['from']
            def record(t,effort='high'):
                return dict(time=t,model='gpt-reserve',effort=effort,usage=tokens())
            for t,model in [(cutover-1,'gpt-5.6-luna'),(cutover,'gpt-6-luna')]:
                raw=record(t)
                self.assertEqual(price_record(raw,rates),price_record(dict(raw,model=model),rates))
                self.assertAlmostEqual(price_record(dict(raw,tier='fast'),rates),price_record(raw,rates)*2)
            self.assertTrue(cache_break_reason(record(cutover-2),record(cutover-1,'max'),rates))
            self.assertIsNone(cache_break_reason(record(cutover),record(cutover+1,'max'),rates))
            self.assertEqual(cache_break_reason(record(cutover-1),record(cutover),rates),'Model change')
            # A later released Luna can be added without repricing earlier requests.
            rates['gpt-6.1-luna']=dict(input=3,cached=1,write=3,output=4)
            rates['gpt-reserve']['history'].append({'from':cutover+100,'model':'gpt-6.1-luna'})
            self.assertEqual(price_record(record(cutover+100),rates),price_record(dict(record(cutover+100),model='gpt-6.1-luna'),rates))
            self.assertEqual(price_record(record(cutover),rates),price_record(dict(record(cutover),model='gpt-6-luna'),rates))

    def test_reserve_subagent_effort_boundary_uses_historical_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            rates=Monitor(folder,Path(folder)/'cache').prices
            cutover=rates['gpt-reserve']['history'][1]['from']
            for t,expected_cached in [(cutover-100,0),(cutover+100,40)]:
                records=lambda time,effort:dict(time=time,model='gpt-reserve',effort=effort,usage=tokens())
                states={'parent':dict(state=dict(records=[records(t,'high')])),
                        'child':dict(state=dict(parent='parent',records=[records(t+2,'max'),records(t+3,'max')]))}
                rows=[dict(id='parent',created_at=t-1),dict(id='child',created_at=t+1)]
                result=cache_pricing_states(rows,states,rates,'switches')['child']['state']['records']
                self.assertEqual(result[0]['usage']['cached_input_tokens'],expected_cached)
                self.assertEqual(result[1]['usage']['cached_input_tokens'],40)
                self.assertEqual(states['child']['state']['records'][0]['usage']['cached_input_tokens'],40)

    def test_mid_chat_tier_changes_and_explicit_standard_preserved(self):
        for tier in ['fast','default']:
            consume(self.state,event('turn_context',dict(model='gpt-6-sol',effort='high',service_tier=tier)),'child')
            consume(self.state,event('token_usage_record',dict(thread_id='child',response_id=tier,usage=tokens())),'child')
        self.assertEqual([r['tier'] for r in self.state['records']],['fast','default'])
        with tempfile.TemporaryDirectory() as folder:
            monitor=Monitor(folder,Path(folder)/'cache')
            monitor.rows=[dict(id='child',title='child',cwd=folder,archived=0,created_at=1,updated_at=1,tokens_used=0)]
            monitor.states={'child':dict(state=self.state)}
            regular=monitor.snapshot();fast=monitor.snapshot(pricing='fast')
            self.assertEqual(regular['total_cost'],fast['total_cost'])
            self.assertEqual(regular['unknown_tier_tokens'],0)


if __name__ == '__main__': unittest.main()
