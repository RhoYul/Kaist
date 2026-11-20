import json, shutil, tempfile, threading, time, unittest
from pathlib import Path
import numpy as np
from fastapi.testclient import TestClient
from studio.core import Store, DEFAULTS, save_json
from studio.exporter import Exporter, CAMERAS
from studio.app import create_app
from studio.capture import Capture
from test_pipeline import raw_episode


def episode_dir(root, name='ep_test'):
    raw = raw_episode(root)                      # raw_episode 는 root/raw 를 만든다 → 에피소드 ID 이름으로 옮긴다
    dest = root / 'episodes' / name; dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(raw), dest); return dest


class FastPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        nas = self.root / 'nas'; nas.mkdir()
        self.cfg = dict(DEFAULTS, role='export', data_dir=str(self.root / 'data'), nas_root=str(nas), demo=True,
                        transfer_token='test-transfer-0123456789', admin_password='test-admin-0123456789', worker_password='test-worker-0123456789')
        self.db = Store(self.cfg['data_dir']); self.ex = Exporter(self.cfg, self.db)
    def tearDown(self): self.ex.pool.shutdown(wait=True); self.tmp.cleanup()
    def wait(self, key, states=('done', 'failed'), timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            j = self.db.get('job', key)
            if j and j['status'] in states: return j
            time.sleep(0.02)
        self.fail('timeout: ' + str(self.db.get('job', key)))

    def test_local_folder_goes_to_nas_without_copies_and_source_untouched(self):
        src = episode_dir(self.root)
        before = {p.relative_to(src).as_posix(): p.read_bytes() for p in src.rglob('*') if p.is_file()}
        self.ex.local_begin('job_l', dict(task_id='task_test', episode_id='ep_test', station='station-01', path=str(src)))
        j = self.wait('job_l'); self.assertEqual(j['status'], 'done', j)
        dest = Path(j['output'])
        self.assertEqual({p.relative_to(dest).as_posix() for p in dest.rglob('*') if p.is_file()},
                         (set(before) - {'episode.json', 'qc.json'}) | {'rgbd_episode.json'})
        for c in CAMERAS: self.assertEqual((dest / (c + '_depth') / 'frame_000001.npy').read_bytes(), before[c + '_depth/frame_000001.npy'])
        self.assertNotIn('confirmed', json.loads((dest / 'extrinsics.json').read_text()))
        self.assertEqual({p.relative_to(src).as_posix(): p.read_bytes() for p in src.rglob('*') if p.is_file()}, before)   # 원본은 그대로
        self.assertFalse((self.root / 'data' / 'incoming').exists())                                                       # ZIP 수신 없음
        self.assertFalse((self.root / 'data' / 'work' / 'job_l').exists())                                                 # 작업 폴더 정리됨

    def test_recording_pauses_nas_work(self):
        src = episode_dir(self.root)
        self.ex.set_recording(True)
        self.ex.local_begin('job_r', dict(task_id='task_test', episode_id='ep_test', station='station-01', path=str(src)))
        time.sleep(0.6)
        j = self.db.get('job', 'job_r'); self.assertNotEqual(j['status'], 'done'); self.assertTrue(j.get('waiting_recording'))
        self.ex.set_recording(False)
        self.assertEqual(self.wait('job_r')['status'], 'done')

    def test_nas_copy_detects_corruption_then_retry_succeeds(self):
        # 복사하면서 계산한 원본 체크섬과 NAS 에 쓰인 내용이 다르면 실패 (폴더는 .part 로만 남는다) → 재시도하면 완료
        import studio.exporter as E
        src = episode_dir(self.root); real = E.copy_hashed
        def broken(s, d):
            r = real(s, d)
            if str(d).endswith('frame_000001.npy'): Path(d).write_bytes(b'x' * r[0])   # 같은 크기 · 다른 내용
            return r
        E.copy_hashed = broken
        try:
            self.ex.local_begin('job_c', dict(task_id='task_test', episode_id='ep_test', station='station-01', path=str(src)))
            j = self.wait('job_c')
        finally: E.copy_hashed = real
        self.assertEqual(j['status'], 'failed'); self.assertIn('NAS 체크섬 불일치', j['error'])
        self.assertFalse([p for p in (self.root / 'nas').iterdir() if not p.name.endswith('.part')])
        self.ex.retry('job_c'); j = self.wait('job_c', states=('done',)); self.assertTrue(Path(j['output']).is_dir())

    def test_serial_and_parallel_copy_give_same_result(self):
        # nas_workers=1 (하나씩) 과 기본값(동시 처리) 의 NAS 결과 · 기록이 같다
        src = episode_dir(self.root); out = {}
        for n in (1, 4):
            nas = self.root / f'nas{n}'; nas.mkdir(); self.cfg.update(nas_root=str(nas), nas_workers=n)
            self.ex.local_begin(f'job_n{n}', dict(task_id='task_test', episode_id='ep_test', station='station-01', path=str(src)))
            j = self.wait(f'job_n{n}'); self.assertEqual(j['status'], 'done', j); dest = Path(j['output'])
            out[n] = (j['files'], j['bytes'], {p.relative_to(dest).as_posix(): p.read_bytes() for p in dest.rglob('*') if p.is_file() and p.name != 'rgbd_episode.json'})   # 표식은 실행마다 새로 만든다 (시각 포함)
        self.assertEqual(out[1][0], out[4][0]); self.assertEqual(out[1][2], out[4][2]); self.assertGreater(out[1][0], 10)

    def test_local_begin_rejects_mismatched_path(self):
        src = episode_dir(self.root, 'ep_other')
        with self.assertRaises(ValueError):
            self.ex.local_begin('job_x', dict(task_id='task_test', episode_id='ep_test', station='station-01', path=str(src)))

    def test_machine_api_for_control_pc(self):
        with TestClient(create_app(self.cfg)) as c:
            h = {'Authorization': 'Bearer ' + self.cfg['transfer_token']}
            self.assertEqual(c.get('/api/machine/overview').status_code, 401)
            body = dict(name='t', scene='2D', target=5, prompt='blue cloth', label='cloth', object_name='blue cloth', interaction='fold')
            t = c.post('/api/machine/tasks', json=body, headers=h).json()
            o = c.get('/api/machine/overview', headers=h).json()
            self.assertEqual([x['id'] for x in o['tasks']], [t['id']]); self.assertTrue(o['nas_reachable'])
            self.assertFalse(c.post('/api/machine/tasks/' + t['id'] + '/toggle', headers=h).json()['active'])
            self.assertEqual(c.post('/api/transfer/job_z/local', json={}, headers=h).status_code, 403)   # 같은 PC 전용


class CaptureStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.cfg = dict(DEFAULTS, role='capture', data_dir=str(self.root / 'cap'), demo=True, export_url='http://127.0.0.1:1',
                        transfer_token='t' * 20, admin_password='a' * 20, worker_password='w' * 20)
        self.db = Store(self.cfg['data_dir']); self.cap = Capture(self.cfg, self.db)
    def tearDown(self): self.cap.executor.shutdown(); self.cap.transfer.shutdown(); self.tmp.cleanup()

    def test_original_deleted_only_after_nas_done(self):
        d = self.root / 'cap' / 'episodes' / 'ep_a'; d.mkdir(parents=True); (d / 'x.npy').write_bytes(b'1')
        self.db.put('episode', 'ep_a', dict(status='qc_done', export_job='job_a', transfer_status='sent'))
        self.cap.reconcile([dict(id='job_a', status='nas_copy')]); self.assertTrue(d.exists())
        self.cap.reconcile([dict(id='job_a', status='failed')]); self.assertTrue(d.exists())
        self.cap.reconcile([dict(id='job_a', status='done', output='//nas/ep_a')]); self.assertTrue(d.exists())   # 옛 형식 저장 기록(files 없음) → 지우지 않음
        self.cap.reconcile([dict(id='job_a', status='done', output='//nas/ep_a', files=3)])
        self.assertFalse(d.exists()); ep = self.db.get('episode', 'ep_a')
        self.assertTrue(ep['local_deleted']); self.assertEqual(ep['transfer_status'], 'done')

    def test_delete_unsent_only(self):
        for k, extra in (('ep_s', dict(export_job='job_s', transfer_status='sent')), ('ep_u', {})):
            (self.root / 'cap' / 'episodes' / k).mkdir(parents=True); self.db.put('episode', k, dict(status='qc_done', **extra))
        with self.assertRaises(ValueError): self.cap.delete_episode('ep_s')
        self.cap.delete_episode('ep_u')
        self.assertFalse((self.root / 'cap' / 'episodes' / 'ep_u').exists()); self.assertEqual(self.db.get('episode', 'ep_u')['status'], 'deleted')

    def test_disk_info_and_local_detection(self):
        d = self.cap.disk()
        self.assertTrue({'free', 'remaining_episodes', 'pending', 'low'} <= set(d))
        self.assertTrue(self.cap.export_is_local())
        self.cap.cfg['export_url'] = 'http://10.0.0.5:8766'; self.assertFalse(self.cap.export_is_local())

    def test_low_disk_blocks_recording(self):
        self.cap.cfg['min_free_gb'] = 10 ** 9   # 어떤 디스크보다 큰 값 → 공간 부족
        self.db.put('calibration', 'current', dict(confirmed=True, serials=['DEMO-' + c for c in CAMERAS]))
        with self.assertRaises(ValueError) as e: self.cap.start(dict(id='t1', name='x', scene='2D'))
        self.assertIn('저장 공간', str(e.exception))

    def test_capture_length_is_5_or_10_seconds(self):
        # 촬영 길이는 5초 · 10초만. 10초면 카메라마다 300장 (30fps), 끝나면 화면 타이머 상태가 비워진다
        self.cap.cfg.update(capture_width=64, capture_height=48, min_free_gb=0)
        self.db.put('calibration', 'current', dict(confirmed=True, serials=['DEMO-' + c for c in CAMERAS]))
        task = dict(id='t1', name='x', scene='2D')
        for bad in (7, 0, 'x'):
            with self.assertRaisesRegex(ValueError, '촬영 길이'): self.cap.start(task, bad)
        ep = self.cap.start(task, 10); self.assertEqual(ep['duration'], 10)
        end = time.monotonic() + 30
        while (self.cap.busy or self.db.get('episode', ep['id'])['status'] not in ('captured', 'failed')) and time.monotonic() < end: time.sleep(0.05)
        done = self.db.get('episode', ep['id'])
        self.assertEqual(done['status'], 'captured', done)
        self.assertEqual(done['frame_counts'], {c: 300 for c in CAMERAS})
        self.assertFalse(self.cap.live_status()['active'])


class ControlTests(unittest.TestCase):
    def test_control_app_local_only_and_login(self):
        from studio.control import create_control_app, normalize_url
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(DEFAULTS, role='control', host='127.0.0.1', port=8767, data_dir=tmp, admin_password='c' * 20)
            with TestClient(create_control_app(cfg)) as c:
                self.assertEqual(c.get('/api/overview').status_code, 401)
                self.assertEqual(c.post('/api/login', json=dict(password='wrong-password')).status_code, 401)
                self.assertEqual(c.post('/api/login', json=dict(password='c' * 20)).status_code, 200)
                self.assertEqual(c.get('/api/overview').json()['pcs'], [])
                r = c.post('/api/pcs', json=dict(name='x', url='127.0.0.1:1', token='t' * 20))   # 연결 안 되는 주소 → 등록 거부
                self.assertEqual(r.status_code, 502); self.assertEqual(c.get('/api/pcs').json(), [])
        self.assertEqual(normalize_url('CAPTURE-01'), 'http://capture-01:8766')
        self.assertEqual(normalize_url('http://10.0.0.5:9000/'), 'http://10.0.0.5:9000')


if __name__ == '__main__':
    unittest.main(verbosity=2)
