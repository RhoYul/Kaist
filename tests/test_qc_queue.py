import json, shutil, tempfile, time, unittest
from pathlib import Path
from studio.core import Store, DEFAULTS, save_json
from studio.exporter import Exporter
from studio.capture import Capture
from test_pipeline import config, raw_episode


def wait_for(fn, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v: return v
        time.sleep(0.05)
    raise AssertionError('timeout')


class ExportQcQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.cfg = config(self.root); self.db = Store(self.cfg['data_dir'])
        raw = raw_episode(self.root); self.src = self.root / 'episodes' / 'ep_test'; self.src.parent.mkdir()
        shutil.move(str(raw), self.src)

    def tearDown(self):
        self.ex.stop_qc(); self.ex.pool.shutdown(wait=True); time.sleep(0.3); self.tmp.cleanup()

    def test_demo_qc_completes(self):
        self.ex = Exporter(self.cfg, self.db)
        self.ex.qc_begin('ep_test', dict(path=str(self.src), scene='2D', station='station-01'))
        q = wait_for(lambda: (self.db.get('qc', 'ep_test') or {}).get('status') == 'done' and self.db.get('qc', 'ep_test'))
        self.assertEqual(q['result']['overall'], 'PASS')

    def test_rejects_bad_path(self):
        self.ex = Exporter(self.cfg, self.db)
        with self.assertRaises(ValueError): self.ex.qc_begin('ep_other', dict(path=str(self.src), scene='2D'))
        with self.assertRaises(ValueError): self.ex.qc_begin('ep_test', dict(path='relative/ep_test', scene='2D'))

    def test_real_qc_process_pauses_for_recording_and_resumes(self):
        # 실제 PhysicalAI QC 를 별도 프로세스로: 녹화가 시작되면 같은 프로세스를 일시정지하고, 끝나면 이어 돌아 한 번에 결과를 낸다
        self.ex = Exporter(dict(self.cfg, demo=False), self.db)
        self.ex.qc_begin('ep_test', dict(path=str(self.src), scene='2D', station='station-01'))
        proc = wait_for(lambda: self.ex.qc_procs.get('ep_test'))
        self.ex.set_recording(True)
        wait_for(lambda: (self.db.get('qc', 'ep_test') or {}).get('waiting_recording'))
        time.sleep(1.0)
        q = self.db.get('qc', 'ep_test')
        self.assertEqual(q['status'], 'running')                       # 종료하지 않았다
        self.assertIsNone(proc.poll())                                 # 녹화 중에는 진행하지 않는다 (결과도 없음)
        self.assertFalse((self.root / 'data' / 'qc' / 'ep_test.json').exists())
        self.ex.set_recording(False)
        q = wait_for(lambda: (self.db.get('qc', 'ep_test') or {}).get('status') in ('done', 'failed') and self.db.get('qc', 'ep_test'), timeout=120)
        self.assertEqual(q['status'], 'done', q.get('error'))
        self.assertEqual((q['attempts'], q['pauses']), (1, 1))         # 처음부터 다시 돌지 않았다
        self.assertGreaterEqual(q['paused_seconds'], 1.0)
        self.assertFalse(q['waiting_recording'])
        self.assertIn(q['result'].get('overall'), ('PASS', 'WARN', 'FAIL'))
        self.assertFalse(list((self.root / 'data' / 'qc').glob('*.json')))

    def test_real_qc_process_stops_for_recording_and_reruns(self):
        # qc_pause=False (예전 방식): 녹화가 시작되면 종료하고, 끝나면 처음부터 다시 돌아 결과를 낸다
        self.ex = Exporter(dict(self.cfg, demo=False, qc_pause=False), self.db)
        self.ex.qc_begin('ep_test', dict(path=str(self.src), scene='2D', station='station-01'))
        wait_for(lambda: 'ep_test' in self.ex.qc_procs)                 # 프로세스 시작됨
        self.ex.set_recording(True)
        wait_for(lambda: 'ep_test' not in self.ex.qc_procs and (self.db.get('qc', 'ep_test') or {}).get('status') == 'queued')
        time.sleep(0.5)
        self.assertEqual(self.db.get('qc', 'ep_test')['status'], 'queued')   # 녹화 중에는 다시 시작하지 않는다
        self.ex.set_recording(False)
        q = wait_for(lambda: (self.db.get('qc', 'ep_test') or {}).get('status') in ('done', 'failed') and self.db.get('qc', 'ep_test'), timeout=120)
        self.assertEqual(q['attempts'], 2)
        self.assertEqual(q['status'], 'done', q.get('error'))
        self.assertIn(q['result'].get('overall'), ('PASS', 'WARN', 'FAIL'))
        self.assertFalse(list((self.root / 'data' / 'qc').glob('*.json')))   # 임시 결과 파일 정리됨


class CaptureBatchFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.cfg = dict(DEFAULTS, role='capture', demo=True, data_dir=str(self.root / 'cap'), max_pending=3,
                        transfer_token='t' * 20, worker_password='w' * 12, admin_password='a' * 12)
        self.db = Store(self.cfg['data_dir']); self.cap = Capture(self.cfg, self.db)
        self.calls = []
        self.cap._export = lambda method, url, timeout=4, **kw: self.calls.append((method, url, kw.get('json'))) or {}
        self.sent = []
        self.cap.send = lambda key: self.sent.append(key)
        for i in range(3):
            k = f'ep_{i}'; (self.root / 'cap' / 'episodes' / k).mkdir(parents=True)
            self.db.put('episode', k, dict(status='captured', scene='2D', created_at=f'2026-11-13T00:00:0{i}', task_id='t', task_name='t'))

    def tearDown(self): self.cap.executor.shutdown(wait=True); self.cap.transfer.shutdown(wait=True); self.tmp.cleanup()

    def test_limit_blocks_recording(self):
        self.db.put('calibration', 'current', dict(confirmed=True, serials=['DEMO-cam_%d' % i for i in range(1, 5)], resolution=self.cap.resolution()))
        with self.assertRaisesRegex(ValueError, '최대 3'): self.cap.start(dict(id='t', name='t', scene='2D'))

    def test_batch_queues_all_on_export_server(self):
        self.assertEqual(self.cap.qc_batch(), 3)
        self.assertEqual([c[1] for c in self.calls], ['/api/qc/ep_0/local', '/api/qc/ep_1/local', '/api/qc/ep_2/local'])
        self.assertTrue(all(self.db.get('episode', f'ep_{i}')['status'] == 'qc_queued' for i in range(3)))
        with self.assertRaises(ValueError): self.cap.qc_batch()          # 더 할 것 없음

    def test_pass_auto_exports_and_warn_waits_for_review(self):
        self.cap.qc_batch()
        res = lambda o: dict(status='done', seconds=1.5, result=dict(ok=True, overall=o, checks=[]))
        self.cap.reconcile_qc([dict(id='ep_0', **res('PASS')), dict(id='ep_1', **res('WARN')), dict(id='ep_2', status='running')])
        e0, e1, e2 = (self.db.get('episode', f'ep_{i}') for i in range(3))
        self.assertEqual((e0['status'], e0['review']['approved'], e0['review']['auto']), ('qc_done', True, True))
        self.assertEqual(self.sent, ['ep_0'])                          # PASS 만 자동 전송
        self.assertEqual(e1['status'], 'qc_done'); self.assertIsNone(e1.get('review'))   # WARN 은 검수 대기
        self.assertEqual(e2['status'], 'qc_running')
        self.assertTrue((self.root / 'cap' / 'episodes' / 'ep_0' / 'qc.json').is_file())
        self.cap.reconcile_qc([dict(id='ep_0', **res('PASS'))])         # 같은 결과를 또 받아도 다시 보내지 않음
        self.assertEqual(self.sent, ['ep_0'])

    def test_failed_qc_returns_to_captured(self):
        self.cap.qc_batch()
        self.cap.reconcile_qc([dict(id='ep_0', status='failed', error='boom')])
        e = self.db.get('episode', 'ep_0'); self.assertEqual(e['status'], 'captured'); self.assertIn('boom', e['error'])


if __name__ == '__main__':
    unittest.main()
