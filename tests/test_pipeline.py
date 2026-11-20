import json, tempfile, unittest, zipfile, time
from pathlib import Path
import numpy as np, cv2
from fastapi.testclient import TestClient
from studio.core import Store, DEFAULTS, save_json, digest
from studio.exporter import Exporter, make_bundle, extract_verified, CAMERAS
from studio.app import create_app
from studio.presets import PRESETS
from studio.vendor.capture_qc import _check_sync, _check_frame_continuity

def config(root,role='export'):
    nas=root/'nas';nas.mkdir(exist_ok=True)
    return dict(DEFAULTS,role=role,data_dir=str(root/'data'),nas_root=str(nas),demo=True,transfer_token='test-transfer-0123456789',admin_password='test-admin-0123456789',worker_password='test-worker-0123456789')

def raw_episode(root):
    p=root/'raw';p.mkdir()
    save_json(p/'episode.json',dict(id='ep_test',task_id='task_test',station='station-01',demo=True,review=dict(approved=True),
                                    object=dict(prompt='blue cloth',label='cloth',scene='2d',name='blue cloth',interaction='fold a cloth by hand')))
    save_json(p/'qc.json',dict(ok=True,overall='PASS'))
    save_json(p/'extrinsics.json',dict(ok=True,method='charuco',transforms={c:dict(R=np.eye(3).tolist(),T=[[0],[0],[0]]) for c in CAMERAS},confirmed=True,serials=['A','B','C','D']))
    for n,cam in enumerate(CAMERAS):
        d=p/(cam+'_depth');d.mkdir()
        w=cv2.VideoWriter(str(p/(cam+'.mp4')),cv2.VideoWriter_fourcc(*'mp4v'),30,(64,48))
        for i in range(3):
            arr=(np.arange(64*48,dtype=np.uint16).reshape(48,64)+i+n)
            np.save(d/('frame_%06d.npy'%i),arr);w.write(np.full((48,64,3),100,np.uint8))
        w.release()
        (p/(cam+'_timestamps.csv')).write_text('frame_idx,hw_timestamp_ms,host_time_s\n0,0,1.0\n1,33.33,1.033333\n2,66.66,1.066666\n')
        save_json(p/(cam+'_intrinsics.json'),dict(color_intrinsics=dict(width=64,height=48,fx=60,fy=60,ppx=32,ppy=24),depth_scale=.001))
    return p

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.cfg=config(self.root)
        self.db=Store(self.cfg['data_dir']);self.ex=Exporter(self.cfg,self.db)
    def tearDown(self): self.ex.pool.shutdown(wait=True);self.tmp.cleanup()
    def upload(self):
        raw=raw_episode(self.root);bundle=self.root/'bundle.zip';info=make_bundle(raw,bundle)
        meta=dict(info,task_id='task_test',episode_id='ep_test',station='station-01')
        return raw,bundle,meta
    def wait_job(self,key):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            j=self.db.get('job',key)
            if j['status'] in ('done','failed'): return j
            time.sleep(.02)
        self.fail('job timed out')
    def test_resume_convert_verify_and_idempotency(self):
        raw,bundle,meta=self.upload();body=bundle.read_bytes();self.ex.begin('job_test',meta)
        self.assertEqual(self.ex.chunk('job_test',0,body[:100])['offset'],100)
        self.assertEqual(self.ex.chunk('job_test',0,body[:100])['offset'],100)
        self.assertEqual(self.ex.begin('job_test',meta)['offset'],100)
        self.ex.chunk('job_test',100,body[100:]);self.ex.complete('job_test')
        j=self.wait_job('job_test');self.assertEqual(j['status'],'done',j)
        dest=Path(j['output'])
        self.assertEqual(dest.parent,Path(self.cfg['nas_root']))           # rgbd_raw 바로 아래
        self.assertRegex(dest.name,r'^ep_\d{8}_\d{6}$')                      # ep_YYYYMMDD_HHMMSS
        # PhysicalAI rgbd_raw 와 같은 녹화 원본 형태: 변환 없이 그대로 + 표식 rgbd_episode.json
        expected={p.relative_to(raw).as_posix() for p in raw.rglob('*') if p.is_file()}-{'episode.json','qc.json'}|{'rgbd_episode.json'}
        self.assertEqual({p.relative_to(dest).as_posix() for p in dest.rglob('*') if p.is_file()},expected)
        for c in CAMERAS:
            for name in (c+'.mp4',c+'_intrinsics.json',c+'_timestamps.csv')+tuple(c+'_depth/frame_%06d.npy'%i for i in range(3)):
                self.assertEqual((dest/name).read_bytes(),(raw/name).read_bytes(),name)
        self.assertEqual(json.loads((dest/'extrinsics.json').read_text())['transforms'].keys(),set(CAMERAS))
        self.assertNotIn('confirmed',json.loads((dest/'extrinsics.json').read_text()))   # 플랫폼 내부 키는 빠진다
        mark=json.loads((dest/'rgbd_episode.json').read_text(encoding='utf-8'))
        self.assertEqual(mark['episodeId'],dest.name);self.assertEqual(mark['task']['taskId'],'task_test')
        self.assertEqual(mark['object'],dict(prompt='blue cloth',label='cloth',scene='2d',name='blue cloth',interaction='fold a cloth by hand'))
        self.assertFalse((dest/'metadata.json').exists())   # PhysicalAI 원본과 같이 metadata.json 은 만들지 않는다 (Phase 1 전처리 표식)
        self.assertEqual(set(mark),{'episodeId','task','object','capture','episodeUid'})
        self.assertEqual(self.ex.complete('job_test')['status'],'done')
    def test_nas_never_overwrites_other_episode_with_same_name(self):
        raw,bundle,meta=self.upload()
        from studio.exporter import nas_folder_name
        ep=json.loads((raw/'episode.json').read_text());ep['started_at']='2026-11-11T12:00:00+00:00';save_json(raw/'episode.json',ep)
        info=make_bundle(raw,self.root/'b2.zip');meta=dict(info,task_id='task_test',episode_id='ep_test',station='station-01')
        existing=Path(self.cfg['nas_root'])/nas_folder_name(ep);existing.mkdir();(existing/'cam_1.mp4').write_bytes(b'other')
        self.ex.begin('job_x',meta);self.ex.chunk('job_x',0,(self.root/'b2.zip').read_bytes());self.ex.complete('job_x')
        j=self.wait_job('job_x');self.assertEqual(j['status'],'failed');self.assertIn('덮어쓰지 않습니다',j['error'])
        self.assertEqual((existing/'cam_1.mp4').read_bytes(),b'other')
    def test_checksum_mismatch(self):
        raw,bundle,meta=self.upload();self.ex.begin('job_test',meta);body=bytearray(bundle.read_bytes());body[-1]^=1
        self.ex.chunk('job_test',0,body)
        with self.assertRaises(ValueError): self.ex.complete('job_test')
    def test_zip_traversal_rejected(self):
        z=self.root/'bad.zip'
        with zipfile.ZipFile(z,'w') as f:f.writestr('../escape.txt','bad');f.writestr('manifest.json','{}')
        with self.assertRaises(ValueError):extract_verified(z,self.root/'out',10000)
        self.assertFalse((self.root/'escape.txt').exists())
    def test_nas_failure_retry(self):
        raw,bundle,meta=self.upload();self.cfg['nas_root']=str(self.root/'missing')
        self.ex.begin('job_test',meta);self.ex.chunk('job_test',0,bundle.read_bytes());self.ex.complete('job_test')
        self.assertEqual(self.wait_job('job_test')['status'],'failed')
        Path(self.cfg['nas_root']).mkdir();self.ex.retry('job_test');self.assertEqual(self.wait_job('job_test')['status'],'done')
    def test_nas_fallback_when_primary_unreachable(self):
        # 다이렉트 랜(nas_root)이 끊기면 예비 경로(nas_root_fallback)로 저장한다
        raw,bundle,meta=self.upload();fallback=self.cfg['nas_root']
        self.cfg['nas_root']=str(self.root/'direct-missing');self.cfg['nas_root_fallback']=[fallback]
        self.ex.begin('job_test',meta);self.ex.chunk('job_test',0,bundle.read_bytes());self.ex.complete('job_test')
        j=self.wait_job('job_test');self.assertEqual(j['status'],'done',j)
        self.assertEqual(Path(j['output']).parent,Path(fallback))
    def test_auth_and_task_permissions(self):
        with TestClient(create_app(self.cfg)) as c:
            self.assertEqual(c.get('/api/state').status_code,401)
            self.assertEqual(c.post('/api/transfer/job_x',json={}).status_code,401)
            self.assertEqual(c.post('/api/login',json=dict(role='worker',password=self.cfg['worker_password'])).status_code,200)
            self.assertEqual(c.post('/api/tasks',json=dict(name='test')).status_code,403)
            c.post('/api/login',json=dict(role='admin',password=self.cfg['admin_password']))
            obj=dict(prompt='blue cloth',label='cloth',object_name='blue cloth',interaction='fold a cloth by hand')
            self.assertEqual(c.post('/api/tasks',json=dict(name='물체 정보 없음',scene='2D',target=10)).status_code,422)   # 다섯 칸 필수
            self.assertEqual(c.post('/api/tasks',json=dict(name='빈칸',scene='2D',target=10,**dict(obj,label='  '))).status_code,400)
            r=c.post('/api/tasks',json=dict(name='테스트 작업',scene='2D',target=10,**obj));self.assertEqual(r.status_code,200)
            from studio.exporter import task_object
            self.assertEqual(task_object(r.json()),{'prompt':'blue cloth','label':'cloth','scene':'2d','name':'blue cloth','interaction':'fold a cloth by hand'})
            self.assertIsNone(task_object(dict(name='old',scene='3D')))     # 예전 작업 → PhysicalAI 처럼 null
            self.assertEqual(c.get('/api/state').json()['tasks'][0]['name'],'테스트 작업')
            self.assertEqual(c.post('/api/tasks',headers={'Origin':'http://evil.test'},json=dict(name='bad')).status_code,403)
    def test_original_qc_sync_and_continuity(self):
        raw=raw_episode(self.root)
        self.assertEqual(_check_sync(str(raw),CAMERAS)[1],'PASS')
        self.assertEqual(_check_frame_continuity(str(raw),CAMERAS,[0,1,2])[1],'PASS')
    def test_duplicate_job_different_source_blocked(self):
        raw,bundle,meta=self.upload();self.ex.begin('job_test',meta)
        with self.assertRaises(ValueError):self.ex.begin('job_test',dict(meta,sha256='a'*64))
    def test_heartbeat_log_deduplication(self):
        with TestClient(create_app(self.cfg)) as client:
            headers={'Authorization':'Bearer '+self.cfg['transfer_token']}
            data=dict(station='station-02',busy='qc',cameras=[],episodes=[],logs=[dict(seq=1,level='info',message='QC test')])
            for _ in range(2): self.assertEqual(client.post('/api/machine/heartbeat',json=data,headers=headers).status_code,200)
            self.assertEqual(len(self.db.logs('station-02')),1)
            self.assertEqual(self.db.get('station','station-02')['busy'],'qc')
    def test_unapproved_episode_cannot_export(self):
        raw=raw_episode(self.root)
        ep=json.loads((raw/'episode.json').read_text());ep['review']['approved']=False;save_json(raw/'episode.json',ep)
        with self.assertRaises(ValueError): make_bundle(raw,self.root/'bad.zip')
    def test_source_qc_unchanged(self):
        project=Path(__file__).resolve().parents[1]
        manifest=json.loads((project/'SOURCE_PROVENANCE.json').read_text())
        entry=next(x for x in manifest if x['file'].endswith('/capture_qc.py'))
        self.assertEqual(digest(project/entry['file']),entry['sha256'])
    def test_nas_folder_name_uses_recording_start_utc(self):
        from studio.exporter import nas_folder_name
        self.assertEqual(nas_folder_name(dict(started_at='2026-11-11T12:54:03.512345+00:00',created_at='2026-11-11T12:53:58+00:00')),'ep_20261111_125403')
        self.assertEqual(nas_folder_name(dict(created_at='2026-11-11T21:53:58+09:00')),'ep_20261111_125358')
    def test_logs_carry_episode_and_can_be_searched(self):
        self.db.log('ep_20261112_010203','QC 완료: PASS')                      # 촬영 PC 에피소드 로그 → episode 자동
        self.db.log('job_a','nas_copy',episode='ep_20261112_999999',station='station-01')
        self.db.log('calibration','ChArUco 계산 완료')
        self.assertEqual([l['message'] for l in self.db.logs(episode='ep_20261112_0102')],['QC 완료: PASS'])
        self.assertEqual(self.db.logs(episode='999999')[0]['station'],'station-01')
        self.assertEqual(len(self.db.logs(q='ChArUco')),1);self.assertEqual(len(self.db.logs(q='ep_20261112')),2)
        with TestClient(create_app(self.cfg)) as c:
            c.post('/api/login',json=dict(role='admin',password=self.cfg['admin_password']))
            r=c.get('/api/logs',params=dict(episode='ep_20261112_0102'));self.assertEqual(r.status_code,200)
            self.assertEqual([l['episode'] for l in r.json()],['ep_20261112_010203'])
    def test_episode_id_is_capture_time_and_nas_folder(self):
        from studio.capture import Capture
        from studio.exporter import episode_mark
        cap=Capture(dict(self.cfg,role='capture',data_dir=str(self.root/'cap')),Store(str(self.root/'cap')))
        try:
            a=cap.new_episode_id();self.assertRegex(a,r'^ep_\d{8}_\d{6}$')
            cap.db.put('episode',a,{});b=cap.new_episode_id()
            self.assertTrue(b==a+'_2' or (b>a and not b.startswith(a)),b)   # 같은 초 → _2 (초가 넘어가면 다음 시각)
            self.assertEqual(episode_mark(dict(id=a,started_at='2020-01-01T00:00:00+00:00'))['episodeId'],a)   # NAS 폴더 = 에피소드 ID
            self.assertEqual(episode_mark(dict(id='ep_6184c8db',started_at='2026-11-11T12:19:47+00:00'))['episodeId'],'ep_20261111_121947')
        finally:
            cap.executor.shutdown();cap.transfer.shutdown()
    def test_shutdown_only_from_this_pc_with_token(self):
        with TestClient(create_app(self.cfg)) as c:   # TestClient 의 접속 주소는 'testclient' (이 PC 가 아님)
            self.assertEqual(c.post('/api/machine/shutdown').status_code,403)
            self.assertEqual(c.post('/api/machine/shutdown',headers={'Authorization':'Bearer '+self.cfg['transfer_token']}).status_code,403)
    def test_session_cookie_is_per_port(self):
        # 같은 PC에서 8765·8766을 함께 열어도 서로의 로그인을 덮어쓰지 않아야 한다
        names=set()
        for port in (8765,8766):
            with TestClient(create_app(dict(self.cfg,port=port,data_dir=str(self.root/str(port))))) as c:
                r=c.post('/api/login',json=dict(role='admin',password=self.cfg['admin_password']))
                names|={k for k in r.cookies.keys()}
                self.assertEqual(c.get('/api/state').status_code,200)
        self.assertEqual(names,{'studio_session_8765','studio_session_8766'})
    def test_state_survives_restart(self):
        self.db.put('job','job_x',dict(status='converting'))
        other=Exporter(self.cfg,Store(self.cfg['data_dir']))
        self.assertEqual(self.db.get('job','job_x')['status'],'failed');other.pool.shutdown(wait=True)

if __name__=='__main__':unittest.main(verbosity=2)
