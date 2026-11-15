"""HTTP, file access, and lifecycle checks for the local operator interface."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from ui import Job, JobManager, ThreadingHTTPServer, capture_config, handler_for, now, write_json


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = JobManager(self.root/'data')
        self.server = ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.manager,'test-local-token'))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=3)
        body = json.dumps(data) if data is not None else None
        connection.request(method,path,body,headers or {})
        response = connection.getresponse()
        code, response_headers, content = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return code, response_headers, content

    def completed_job(self, successful=True):
        job_root = self.manager.root/'ui-test'
        session = job_root/'session'
        (session/'csv').mkdir(parents=True)
        job = Job('ui-test',job_root,dict(targets=['test-market'],minutes=1),now(),finished_at=now(),returncode=0)
        write_json(session/'metadata.json',dict(markets=[]))
        write_json(session/'run_summary.json',dict(successful=successful))
        path = session/'csv'/'quotes.csv'
        path.write_text('price,size\n0.5,10\n')
        self.manager.jobs[job.id] = job
        self.manager._persist(job)
        return job, session

    def test_home_and_scripts_are_served_with_local_token(self):
        code, headers, body = self.request('GET','/')
        self.assertEqual(code,200)
        self.assertIn(b'test-local-token',body)
        self.assertIn('no-store',headers['Cache-Control'])
        self.assertEqual(self.request('GET','/app.js')[0],200)
        self.assertEqual(self.request('GET','/app.css')[0],200)

    def test_empty_state_has_no_fake_results(self):
        code, _, body = self.request('GET','/api/state')
        self.assertEqual(code,200)
        value = json.loads(body)
        self.assertIsNone(value['current'])
        self.assertEqual(value['jobs'],[])

    def test_external_host_is_rejected(self):
        self.assertEqual(self.request('GET','/api/state',headers={'Host':'evil.example'})[0],403)

    def test_start_requires_local_token_and_matching_origin(self):
        data=dict(targets=['test-market'],minutes=1)
        self.assertEqual(self.request('POST','/api/start',data)[0],403)
        self.assertEqual(self.request('POST','/api/start',data,{'X-Local-Token':'test-local-token','Origin':'https://evil.example'})[0],403)

    def test_bad_configuration_does_not_start_a_process(self):
        with patch('ui.subprocess.Popen') as process:
            code, _, _ = self.request('POST','/api/start',dict(targets=[],minutes=80),{'X-Local-Token':'test-local-token'})
            self.assertEqual(code,400)
            process.assert_not_called()

    def test_duplicate_capture_is_rejected(self):
        self.manager.active_id='already-running'
        code, _, _ = self.request('POST','/api/start',dict(targets=['test-market'],minutes=1),{'X-Local-Token':'test-local-token'})
        self.assertEqual(code,409)

    def test_download_uses_only_known_result_files(self):
        job, _ = self.completed_job()
        query = urlencode(dict(job=job.id,file='csv/quotes.csv'))
        code, headers, body = self.request('GET','/api/download?'+query)
        self.assertEqual(code,200)
        self.assertEqual(body,b'price,size\n0.5,10\n')
        self.assertEqual(int(headers['Content-Length']),len(body))
        self.assertIn('attachment',headers['Content-Disposition'])
        query = urlencode(dict(job=job.id,file='../../private.txt'))
        self.assertEqual(self.request('GET','/api/download?'+query)[0],404)

    def test_symlink_cannot_expose_an_outside_file(self):
        job, session = self.completed_job()
        secret = self.root/'outside.csv'
        secret.write_text('private')
        (session/'csv'/'outside.csv').symlink_to(secret)
        query = urlencode(dict(job=job.id,file='csv/outside.csv'))
        self.assertEqual(self.request('GET','/api/download?'+query)[0],404)

    def test_history_is_restored_and_missing_summary_is_not_success(self):
        job, session = self.completed_job()
        restored = JobManager(self.manager.root)
        self.assertEqual(restored.snapshot()['current']['state'],'complete')
        (session/'run_summary.json').unlink()
        self.assertEqual(restored.snapshot()['current']['state'],'failed')

    def test_search_returns_rows_for_the_picker(self):
        expected=[dict(slug='test-market',question='Test?',event_title='Election')]
        with patch('ui.search_markets',return_value=expected):
            code, _, body = self.request('GET','/api/search?q=election')
        self.assertEqual(code,200)
        self.assertEqual(json.loads(body)['markets'],expected)

    def test_invalid_fee_and_latency_are_rejected(self):
        for fields in [dict(fee_cents='NaN'),dict(latency_ms=100),dict(minutes=float('inf'))]:
            with self.assertRaises(ValueError):
                capture_config(dict(targets=['test-market'],**fields))

    def test_stop_does_not_interrupt_csv_processing(self):
        job, session = self.completed_job()
        job.finished_at=None
        write_json(session/'status.json',dict(state='complete'))
        with self.assertRaises(RuntimeError):
            self.manager.stop(job.id)
        self.assertFalse(job.stop_requested)


if __name__ == '__main__':
    unittest.main()
