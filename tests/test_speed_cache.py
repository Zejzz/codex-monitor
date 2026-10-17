import copy
import json
import unittest
from monitor import diagnostic_tier, recover_tiers, tier_metadata, speed_category, cache_break_reason, cache_pricing_states


class SpeedCacheTests(unittest.TestCase):
    def test_presence_is_distinct(self):
        for payload,expected in [({},'absent'),({'service_tier':None},'null'),({'speed':''},'empty'),
                                  ({'fast_mode':False},'standard'),({'fast_mode':True},'fast'),({'speed':'flex'},'other')]:
            self.assertEqual(speed_category(tier_metadata(payload)),expected)

    def test_structured_tags_and_exact_request_matching(self):
        body='run_turn{turn_id=t model=gpt-6-sol} tags_json='+json.dumps(json.dumps({'service_tier':'priority'}))
        item=diagnostic_tier(body)
        self.assertEqual(item['tier'],'priority')
        self.assertIsNone(diagnostic_tier('user said service_tier=priority'))
        evidence={('chat','t'):[dict(item,time=5)]}
        records=[dict(time=10,turn_id='t',model='gpt-6-sol',tier='unknown',tier_presence='absent'),
                 dict(time=4,turn_id='t',model='gpt-6-sol',tier='unknown'),
                 dict(time=10,turn_id='other',model='gpt-6-sol',tier='unknown'),
                 dict(time=10,turn_id='t',model='gpt-6.1-sol',tier='unknown'),
                 dict(time=10,turn_id='t',model='gpt-6-sol',tier='default',tier_presence='present')]
        state={'records':records}; original=copy.deepcopy(state)
        result=recover_tiers(state,'chat',evidence)
        self.assertEqual([r['tier'] for r in result['records']],['priority','unknown','unknown','unknown','default'])
        self.assertEqual(recover_tiers(state,'different-chat',evidence),state)
        self.assertEqual(state,original)

    def test_old_effort_new_effort_and_model_switches(self):
        def config(model,effort):return dict(model=model,effort=effort,time=1)
        self.assertTrue(cache_break_reason(config('gpt-5.6-sol','high'),config('gpt-5.6-sol','low'),{}))
        self.assertIsNone(cache_break_reason(config('gpt-6-sol','high'),config('gpt-6-sol','low'),{}))
        self.assertTrue(cache_break_reason(config('gpt-6-sol','high'),config('gpt-6.1-sol','high'),{}))
        self.assertIsNone(cache_break_reason(config('unknown','high'),config('gpt-6-sol','high'),{}))

    def test_subagent_boundary_only_first_request_repriced_without_mutation(self):
        def record(model,t):return dict(time=t,model=model,effort='high',usage=dict(cached_input_tokens=80,input_tokens=100))
        states={'parent':{'state':{'records':[record('gpt-5.6-sol',1)]}},
                'child':{'state':{'parent':'parent','records':[record('gpt-6-sol',3),record('gpt-6-sol',4)]}}}
        rows=[{'id':'parent','created_at':0},{'id':'child','created_at':2}]; original=copy.deepcopy(states)
        result=cache_pricing_states(rows,states,{},'switches')['child']['state']['records']
        self.assertTrue(result[0]['cache_break_from_parent'])
        self.assertEqual([r['usage']['cached_input_tokens'] for r in result],[0,80])
        self.assertEqual(cache_pricing_states(rows,states,{},'recorded')['child']['state']['records'][0]['usage']['cached_input_tokens'],80)
        self.assertEqual(states,original)


if __name__=='__main__':unittest.main()
