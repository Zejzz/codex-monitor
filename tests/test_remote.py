import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from collector import Uploader,validate_endpoint,failure_description
from monitor import Monitor,blank
from remote_sync import RemoteStore,clean_thread,make_receiver


def item(identifier='remote-chat',model='gpt-6.1-sol',when=None):
    when=when or time.time()
    state=blank();state.update(model=model,effort='high',latest=when)
    state['records']=[dict(model=model,effort='high',time=when,source='request',usage=dict(
        input_tokens=100,cached_input_tokens=40,output_tokens=10,reasoning_output_tokens=3,
        cache_write_input_tokens=0,total_tokens=110))]
    row=dict(id=identifier,title='Remote chat',cwd='/remote/project',created_at=when-100,
             updated_at=when,tokens_used=110,archived=False,source='vscode')
    return dict(row=row,state=state)


class RemoteTests(unittest.TestCase):
    def test_failure_diagnostics_do_not_leak_credentials_or_response_text(self):
        import socket
        private='https://private.example/?token=secret-upload-key'
        auth=urllib.error.HTTPError(private,401,'private response',{},None)
        self.assertEqual(failure_description(auth),'HTTP 401: upload key rejected')
        auth.close()
        self.assertEqual(failure_description(urllib.error.URLError(TimeoutError(private))),'connection timed out')
        self.assertEqual(failure_description(urllib.error.URLError(socket.gaierror(private))),'hostname could not be resolved')
        self.assertNotIn('secret',failure_description(urllib.error.URLError(private)))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.config=self.root/'config.json'
        self.config.write_text(json.dumps(dict(device_tokens={'paired-device':'test-key'})),encoding='utf-8')
        self.store=RemoteStore(self.root/'remote.sqlite')
        self.server=make_receiver(self.store,self.config,'127.0.0.1',0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        self.uploader=Uploader(dict(endpoint=self.url,device_id='paired-device',token='test-key',device_name='Test laptop'))

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def test_upload_is_authenticated_and_idempotent_and_persists(self):
        record=item()
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.url+'/api/health')
        self.assertEqual(e.exception.code,401)
        e.exception.close()
        self.assertEqual(self.uploader.sync([record]),1)
        self.assertEqual(self.uploader.sync([record]),0)
        restarted=RemoteStore(self.store.path)
        self.assertEqual(len(restarted.threads),1)
        monitor=Monitor(self.root,self.root/'cache.sqlite');monitor.remote=restarted
        snap=monitor.snapshot()
        self.assertEqual(snap['total_tokens'],110)
        self.assertEqual(snap['threads'][0]['device_name'],'Test laptop')
        self.assertEqual(monitor.model_snapshot()['models'][0]['tokens'],110)

    def test_remote_only_host_starts_without_local_history_and_counts_uploaded_usage(self):
        self.uploader.sync([item()])
        monitor=Monitor(self.root/'no-codex-home',self.root/'cache.sqlite',remote_only=True)
        monitor.remote=self.store
        running=threading.Thread(target=monitor.run,daemon=True)
        running.start()
        try:
            deadline=time.monotonic()+2
            while monitor.data['indexing'] and time.monotonic()<deadline:time.sleep(.01)
            snap=monitor.snapshot()
            self.assertFalse(snap['indexing'])
            self.assertEqual(snap['errors'],[])
            self.assertEqual(snap['total_tokens'],110)
            self.assertEqual(snap['home'],'Remote collectors')
            self.assertEqual([d['name'] for d in snap['devices']],['Test laptop'])
            self.assertFalse((self.root/'no-codex-home').exists())
        finally:
            monitor.stop.set();running.join(2)

    def test_partial_retry_replaces_thread_and_old_revision_cannot_overwrite(self):
        original=item()
        self.uploader.sync([original])
        changed=copy.deepcopy(original)
        changed['state']['records'].append(copy.deepcopy(original['state']['records'][0]))
        self.uploader.sync([changed])
        self.assertEqual(len(self.store.threads[('paired-device','remote-chat')]['state']['records']),2)
        result=self.store.ingest(dict(version=1,device_id='paired-device',device_name='Test laptop',
            revision=1,threads=[original]),'paired-device')
        self.assertEqual(result['accepted'],0)
        self.assertEqual(len(self.store.threads[('paired-device','remote-chat')]['state']['records']),2)

    def test_readonly_snapshot_keeps_old_history_across_upload_replacement(self):
        original=item();self.uploader.sync([original])
        _,prior=self.store.snapshot_refs()
        changed=copy.deepcopy(original)
        changed['state']['records'][0]['usage'].update(input_tokens=200,total_tokens=210)
        self.uploader.sync([changed])
        self.assertEqual(prior[('paired-device','remote-chat')]['state']['records'][0]['usage']['input_tokens'],100)
        self.assertEqual(self.store.threads[('paired-device','remote-chat')]['state']['records'][0]['usage']['input_tokens'],200)
        monitor=Monitor(self.root,self.root/'cache.sqlite');monitor.remote=self.store
        monitor.snapshot(pricing='fast',cache_policy='switches')
        self.assertEqual(self.store.threads[('paired-device','remote-chat')]['state']['records'][0]['tier'],'unknown')

    def test_device_key_cannot_impersonate_another_device(self):
        impostor=Uploader(dict(endpoint=self.url,device_id='different-device',token='test-key'))
        with self.assertRaises(urllib.error.HTTPError) as e:impostor.sync([item()])
        self.assertEqual(e.exception.code,401)
        e.exception.close()

    def test_unrecognized_fields_are_not_stored(self):
        raw=item();raw['row']['first_user_message']='private message';raw['state']['auth']='secret'
        clean=clean_thread(raw)
        self.assertNotIn('first_user_message',clean['row']);self.assertNotIn('auth',clean['state'])

    def test_invalid_batch_is_atomic_and_browser_posts_are_rejected(self):
        broken=item('bad');broken['state']['records'][0]['usage']['total_tokens']=999
        with self.assertRaises(ValueError):
            self.store.ingest(dict(version=1,device_id='paired-device',device_name='Laptop',
                revision=time.time(),threads=[item(),broken]),'paired-device')
        self.assertEqual(self.store.threads,{})
        req=urllib.request.Request(self.url+'/api/ingest',data=b'{}',headers=dict(
            self.uploader.headers,Origin='https://example.com'))
        with self.assertRaises(urllib.error.HTTPError) as e:self.uploader.opener.open(req)
        self.assertEqual(e.exception.code,403)
        e.exception.close()

    def test_duplicate_chat_across_devices_and_per_device_filter(self):
        raw=item();self.uploader.sync([raw])
        monitor=Monitor(self.root,self.root/'cache.sqlite');monitor.remote=self.store
        monitor.rows=[raw['row']];monitor.states={'remote-chat':dict(state=raw['state'])}
        monitor.edges={};monitor.history={}
        self.assertEqual(monitor.snapshot()['total_tokens'],110)
        self.assertEqual(monitor.snapshot('paired-device')['total_tokens'],110)
        self.assertEqual(monitor.snapshot('local')['total_tokens'],110)
        self.assertEqual(monitor.model_snapshot(device='paired-device')['models'][0]['tokens'],110)

    def test_offline_device_is_not_shown_running(self):
        raw=item();raw['state']['turns']={'turn':dict(start=time.time()-200,end=None,status='inProgress')}
        self.uploader.sync([raw]);self.store.devices['paired-device']['seen']=time.time()-120
        monitor=Monitor(self.root,self.root/'cache.sqlite');monitor.remote=self.store
        snap=monitor.snapshot()
        self.assertEqual(snap['active'],0);self.assertTrue(snap['threads'][0]['sync_stale'])

    def test_endpoint_validation_and_bind_restriction(self):
        for endpoint in ['http://example.com','http://192.168.1.20','https://example.com','http://user:pass@100.64.0.10']:
            with self.assertRaises(ValueError):validate_endpoint(endpoint)
        self.assertEqual(validate_endpoint('http://100.64.0.10:8767'),'http://100.64.0.10:8767')
        with self.assertRaises(ValueError):make_receiver(self.store,self.config,'0.0.0.0',0)


if __name__=='__main__':unittest.main()
