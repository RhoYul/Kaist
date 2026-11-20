from __future__ import annotations
import hashlib, json, os, re, shutil, socket, subprocess, sys, threading, time, uuid, zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
import cv2
from .core import digest, save_json, safe_id, now

CAMERAS = ['cam_1','cam_2','cam_3','cam_4']
# NAS(rgbd_raw)에는 PhysicalAI 와 같은 녹화 원본 폴더만 올린다. 아래는 플랫폼 내부용(촬영 PC · export 서버에만 남김)
LOCAL_ONLY = ('episode.json','qc.json')
# PhysicalAI 가 에피소드 폴더에 복사하는 extrinsics.json 의 키 (캘리브레이션 결과 + saved_at)
EXTRINSICS_KEYS = ('ok','method','cameras','transforms','intrinsics','reprojectionErrors','saved_at')
EPISODE_MARK = 'rgbd_episode.json'

# ── NAS 경로 선택: nas_root(예: 다이렉트 랜) 먼저, 안 되면 nas_root_fallback(예: 사내망 · Wi-Fi) 순서로 ──
# 같은 NAS 의 같은 공유 폴더를 다른 주소로 가리키는 것만 넣는다 (폴더 내용이 같아야 덮어쓰기 방지 확인이 맞다).
def nas_candidates(cfg):
    out=[]
    fb=cfg.get('nas_root_fallback') or []
    if isinstance(fb,str): fb=[fb]
    for r in [cfg.get('nas_root')]+list(fb):
        r=str(r or '').strip()
        if r and r not in out: out.append(r)
    return out

def _smb_host_up(root,timeout=1.0):
    """UNC 경로(\\\\서버\\공유)면 SMB 445 포트를 먼저 짧게 확인한다. 끊긴 주소에 is_dir() 하면 수십 초 멈추기 때문."""
    m=re.match(r'^[\\/]{2}([^\\/]+)',root)
    if not m: return True
    try:
        socket.create_connection((m.group(1),445),timeout).close(); return True
    except OSError: return False

def resolve_nas(cfg):
    """쓸 수 있는 첫 NAS 경로 (Path) 또는 None."""
    for r in nas_candidates(cfg):
        try:
            if _smb_host_up(r) and Path(r).is_dir(): return Path(r)
        except OSError: pass
    return None

_nas_cache={}
def resolve_nas_cached(cfg,ttl=10.0):
    """상태 표시용 (화면이 자주 묻는다). 실제 저장은 resolve_nas 로 매번 확인한다."""
    key=tuple(nas_candidates(cfg)); hit=_nas_cache.get(key)
    if hit and time.time()-hit[0]<ttl: return hit[1]
    p=resolve_nas(cfg); _nas_cache[key]=(time.time(),p); return p

def copy_hashed(src,dst):
    """원본을 한 번만 열어 복사하면서 SHA256 을 같이 계산한다 → (크기, sha256).
    파일이 작고 많아(에피소드당 depth .npy 약 1,100개) 전송량보다 파일을 여는 시간이 더 크다. 해시용 · 복사용으로 두 번 열지 않는다."""
    h=hashlib.sha256(); size=0
    with open(src,'rb') as s,open(dst,'wb') as d:
        for block in iter(lambda: s.read(1024*1024), b''):
            h.update(block); d.write(block); size+=len(block)
    shutil.copystat(src,dst)   # 수정 시각 유지 (예전 shutil.copy2 와 같게)
    return size,h.hexdigest()

# ── QC 프로세스 일시정지 · 이어 돌리기 (녹화 우선) ──
def _proc_suspend(proc,on):
    """프로세스를 통째로 멈추거나(on=True) 이어 돌린다. 멈춘 동안에는 CPU · 디스크를 쓰지 않는다. 실패하면 False."""
    try:
        if os.name=='nt':
            import ctypes
            fn=getattr(ctypes.windll.ntdll,'NtSuspendProcess' if on else 'NtResumeProcess')
            fn.argtypes=[ctypes.c_void_p]; fn.restype=ctypes.c_long
            return fn(int(proc._handle))==0
        import signal
        proc.send_signal(signal.SIGSTOP if on else signal.SIGCONT); return True
    except Exception: return False

_qc_job=None
def _qc_job_add(proc):
    """export 서버가 어떻게 끝나든(강제 종료 포함) QC 프로세스도 같이 끝나게 한다 (Windows Job).
    멈춰 둔 QC 프로세스가 남아 원본 폴더를 계속 잡고 있지 않게. 실패해도 QC 는 그대로 돈다."""
    global _qc_job
    if os.name!='nt': return
    try:
        import ctypes
        from ctypes import wintypes as w
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        if _qc_job is None:
            class Basic(ctypes.Structure):
                _fields_=[('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),('LimitFlags',w.DWORD),
                          ('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',w.DWORD),
                          ('Affinity',ctypes.c_size_t),('PriorityClass',w.DWORD),('SchedulingClass',w.DWORD)]
            class Extended(ctypes.Structure):
                _fields_=[('Basic',Basic),('Io',ctypes.c_uint64*6),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),
                          ('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
            k.CreateJobObjectW.restype=w.HANDLE; k.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR]
            k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
            job=k.CreateJobObjectW(None,None)
            info=Extended(); info.Basic.LimitFlags=0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not job or not k.SetInformationJobObject(job,9,ctypes.byref(info),ctypes.sizeof(info)): return
            _qc_job=job
        k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        k.AssignProcessToJobObject(_qc_job,int(proc._handle))
    except Exception: pass

def public_extrinsics(cal):
    return {k:cal[k] for k in EXTRINSICS_KEYS if k in cal}

def _utc_naive(stamp):
    """PhysicalAI 표식의 시각 형식: UTC, 시간대 표기 없음 (datetime.utcnow().isoformat())."""
    from datetime import datetime, timezone
    if not stamp: return None
    t=datetime.fromisoformat(stamp)
    if t.tzinfo is not None: t=t.astimezone(timezone.utc).replace(tzinfo=None)
    return t.isoformat()

OBJECT_FIELDS = ('prompt','label','scene','name','interaction')   # PhysicalAI rgbd_process/store.py 와 같은 순서

def task_object(task):
    """작업의 물체 정보 → PhysicalAI rgbd_episode.json 'object' 형식 (다섯 칸 문자열, scene 은 1d/2d/3d 소문자).
    물체 정보가 없는 예전 작업이면 None (PhysicalAI 도 물체 정보가 없으면 null 을 쓴다)."""
    if not task or not str(task.get('prompt') or '').strip():
        return None
    return {'prompt': str(task.get('prompt','')).strip(), 'label': str(task.get('label','')).strip(),
            'scene': str(task.get('scene','')).strip().lower(), 'name': str(task.get('object_name','')).strip(),
            'interaction': str(task.get('interaction','')).strip()}


def episode_mark(ep, uid=None):
    """PhysicalAI 와 같은 형식의 rgbd_episode.json. 중계기 프로젝트 · 작업자 ID 는 이 시스템에 없어 비운다."""
    cal=ep.get('calibration') or {}
    # 촬영 PC 의 에피소드 ID 가 이미 ep_YYYYMMDD_HHMMSS 형식이면 그대로 쓴다 (NAS 폴더 = 에피소드 ID). 예전 무작위 ID 는 시작 시각으로 만든다
    eid=str(ep.get('id') or '')
    episode_id=eid if re.fullmatch(r'ep_\d{8}_\d{6}(_\d+)?',eid) else nas_folder_name(ep)
    return {
        'episodeId': episode_id,
        'task': {'taskId': ep.get('task_id'), 'taskName': ep.get('task_name') or '', 'projectId': None, 'workerId': None, 'orgId': None},
        'object': ep.get('object'),
        'capture': {
            'startedAt': _utc_naive(ep.get('started_at')),
            'calibration': {'present': True, 'savedAt': _utc_naive(cal.get('saved_at')), 'method': cal.get('method','charuco')} if cal else {'present': False},
            'stoppedAt': _utc_naive(ep.get('ended_at')),
            'cameras': list(CAMERAS),
            'frameCounts': {c:(ep.get('frame_counts') or {})[c] for c in CAMERAS if c in (ep.get('frame_counts') or {})},
            'failed': [],
        },
        'episodeUid': uid or ep.get('episode_uid') or str(uuid.uuid4()),
    }

def nas_folder_name(ep):
    """NAS 폴더 이름. PhysicalAI 와 같은 규칙: 녹화 시작 시각(UTC) 'ep_YYYYMMDD_HHMMSS'."""
    from datetime import datetime, timezone
    stamp=ep.get('started_at') or ep.get('created_at')
    t=datetime.fromisoformat(stamp) if stamp else datetime.now(timezone.utc)
    if t.tzinfo is None: t=t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).strftime('ep_%Y%m%d_%H%M%S')

def validate_raw(p):
    """Transport integrity checks, separate from the unchanged scientific QC."""
    p=Path(p)
    for name in ('extrinsics.json','episode.json','qc.json'):
        if not (p/name).is_file(): raise ValueError('필수 파일 없음: '+name)
    ep=json.loads((p/'episode.json').read_text(encoding='utf-8'))
    if not ep.get('review',{}).get('approved'): raise ValueError('촬영 검수 승인 필요')
    for cam in CAMERAS:
        for name in (cam+'.mp4',cam+'_intrinsics.json',cam+'_timestamps.csv'):
            if not (p/name).is_file(): raise ValueError('필수 파일 없음: '+name)
        depths=sorted((p/(cam+'_depth')).glob('frame_*.npy'))
        if not depths: raise ValueError(cam+': Depth 없음')
        cap=cv2.VideoCapture(str(p/(cam+'.mp4')))
        count=int(cap.get(cv2.CAP_PROP_FRAME_COUNT)); cap.release()
        lines=(p/(cam+'_timestamps.csv')).read_text().strip().splitlines()
        if count!=len(depths) or len(lines)-1!=len(depths):
            raise ValueError(cam+': RGB/Depth/시각 개수 불일치')
    return ep

def make_bundle(source, target):
    source=Path(source); target=Path(target)
    validate_raw(source)
    files=sorted(p for p in source.rglob('*') if p.is_file())
    entries=[]
    for p in files:
        if p.is_symlink(): raise ValueError('심볼릭 링크는 전송할 수 없습니다')
        entries.append(dict(path=p.relative_to(source).as_posix(),size=p.stat().st_size,sha256=digest(p)))
    manifest=dict(version=1,files=entries)
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_suffix('.part')
    with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as z:
        z.writestr('manifest.json',json.dumps(manifest))
        for e in entries: z.write(source/e['path'],e['path'])
    temp.replace(target)
    return dict(size=target.stat().st_size,sha256=digest(target))

def extract_verified(bundle, out, limit):
    out=Path(out); out.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(bundle) as z:
        infos=z.infolist(); names=[x.filename for x in infos]
        if len(names)!=len(set(names)) or len(names)>20000: raise ValueError('잘못된 묶음 파일 목록')
        if sum(x.file_size for x in infos)>limit: raise ValueError('압축 해제 크기 초과')
        for i in infos:
            p=PurePosixPath(i.filename)
            if p.is_absolute() or '..' in p.parts or ':' in i.filename or '\\' in i.filename or i.is_dir() or (i.external_attr>>16)&0o170000==0o120000:
                raise ValueError('허용되지 않은 묶음 경로')
        if 'manifest.json' not in names: raise ValueError('전송 명세 없음')
        if z.getinfo('manifest.json').file_size>8*1024**2: raise ValueError('전송 명세 크기 초과')
        m=json.loads(z.read('manifest.json'))
        entries=m['files']
        if len({x['path'] for x in entries})!=len(entries): raise ValueError('중복 명세')
        if {x['path'] for x in entries} != set(names)-{'manifest.json'}: raise ValueError('전송 명세 불일치')
        for e in entries:
            dest=out/e['path']; dest.parent.mkdir(parents=True,exist_ok=True)
            with z.open(e['path']) as s,open(dest,'wb') as d: shutil.copyfileobj(s,d,1024*1024)
            if dest.stat().st_size!=e['size'] or digest(dest)!=e['sha256']: raise ValueError('전송 파일 손상: '+e['path'])
    return validate_raw(out)

class Exporter:
    def __init__(self, cfg, db):
        self.cfg,self.db=cfg,db
        self.root=Path(cfg['data_dir']); self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='export')
        self.locks={}; self.lock=threading.RLock()
        # 에피소드 ID 칸이 생기기 전의 작업 로그는 작업 기록으로 채운다 (관리자 로그 탐색용)
        with db.connect() as c:
            for j in db.all('job'):
                if j.get('episode_id'):
                    c.execute('UPDATE events SET episode=?, station=? WHERE entity=? AND episode IS NULL',(j['episode_id'],j.get('station'),j['id']))
        for j in db.all('job'):
            if j['status'] in ('queued','extracting','converting','nas_copy','verifying'):
                # 같은 PC 원본은 촬영 PC 에 그대로 있으니 다시 이어서 저장한다. ZIP 으로 받은 것은 수동 재시도
                if j.get('mode')=='local' and Path(j.get('source','')).is_dir():
                    db.patch('job',j['id'],status='queued',error=None,progress=0); self.pool.submit(self.process,j['id']); continue
                db.patch('job',j['id'],status='failed',error='서버가 재시작되었습니다. 재시도하세요.')
        self._qc_init()
    def log(self,key,message,level='info'):
        j=self.db.get('job',key) or {}   # 작업 로그에 에피소드 ID · 촬영 PC 를 같이 남긴다 (관리자 화면 탐색용)
        self.db.log(key,message,level,episode=j.get('episode_id'),station=j.get('station'))
    # ── 녹화 우선: 촬영 PC 가 녹화 시작 · 끝을 알려 준다. 녹화 중에는 디스크를 읽고 쓰는 일을 잠깐 멈춘다 ──
    recording_until=0.0
    def set_recording(self,active):
        self.recording_until=time.time()+20 if active else 0.0   # 끝 신호를 못 받아도 20초 뒤 저절로 풀린다
    def wait_recording(self,key=None):
        waited=False
        while time.time()<self.recording_until:
            if key and not waited: self.db.patch('job',key,waiting_recording=True); waited=True
            time.sleep(0.2)
        if waited: self.db.patch('job',key,waiting_recording=False)
    # ── QC 대기열: 같은 PC 촬영 원본의 QC 를 여기서 돌린다 (촬영 서버는 촬영만) ──
    # PhysicalAI 원본 QC 를 별도 프로세스(studio.qc_worker)로 낮은 우선순위에서 실행한다.
    # 녹화가 시작되면 돌던 QC 프로세스를 바로 일시정지하고 녹화가 끝나면 멈춘 곳부터 이어 돌린다 → 녹화 프레임에 영향 없음.
    # (예전에는 종료 후 처음부터 다시 돌렸다. QC 1회 약 28초가 연속 촬영 간격보다 길어 대부분 3번, 많게는 10번 만에 끝났다.
    #  설정 qc_pause=false 이거나 일시정지를 못 하면 예전 방식으로 돈다.)
    def _qc_init(self):
        self.qc_pool=ThreadPoolExecutor(max_workers=max(1,int(self.cfg.get('qc_workers',3))),thread_name_prefix='qc')
        self.qc_procs={}; self.qc_paused={}; self.qc_stopping=False
        for q in self.db.all('qc'):
            if q['status'] in ('queued','running'):
                if Path(q.get('source','')).is_dir():
                    self.db.patch('qc',q['id'],status='queued'); self.qc_pool.submit(self._qc_run,q['id'])
                else: self.db.patch('qc',q['id'],status='failed',error='원본 폴더가 없습니다 (서버 재시작 중 삭제됨)')
    def qc_begin(self,key,meta):
        from .presets import PRESETS
        safe_id(key); scene=meta.get('scene','3D')
        if scene not in PRESETS: raise ValueError('장면 유형 오류')
        src=Path(meta['path'])
        if not src.is_absolute() or not src.is_dir() or src.name!=key: raise ValueError('원본 폴더 경로 오류')
        with self.lock_for('qc_'+key):
            q=self.db.get('qc',key)
            if q and q['status'] in ('queued','running'): return q
            q=self.db.put('qc',key,dict(episode_id=key,source=str(src),scene=scene,station=meta.get('station'),status='queued',
                                        created_at=now(),attempts=0,result=None,error=None,seconds=None))
        self.db.log(key,'QC 대기열 등록',episode=key,station=meta.get('station'))
        self.qc_pool.submit(self._qc_run,key)
        return q
    def _qc_once(self,key,q,out):
        """QC 한 번. 녹화 중에는 프로세스를 일시정지했다가 이어 돌린다. 종료해서 다시 돌려야 하면 None."""
        if self.cfg['demo']:
            time.sleep(0.5)
            return dict(ok=True,overall='PASS',demo=True,checks=[dict(name='demo',label='데모 데이터',status='PASS',
                        detail='UI · 자동 전송 검증용 모의 결과. 실제 QC 알고리즘 실행 결과가 아닙니다.')])
        out.unlink(missing_ok=True); err=out.with_suffix('.err.log')
        flags=getattr(subprocess,'BELOW_NORMAL_PRIORITY_CLASS',0)|getattr(subprocess,'CREATE_NO_WINDOW',0)
        with open(err,'wb') as ef:
            proc=subprocess.Popen([sys.executable,'-m','studio.qc_worker',q['source'],q['scene'],str(out)],cwd=str(Path(__file__).resolve().parents[1]),
                                  stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=ef,creationflags=flags)
            self.qc_procs[key]=proc; _qc_job_add(proc)
            self.qc_paused[key]=dict(seconds=0.0,count=0); since=None
            try:
                while proc.poll() is None:
                    if self.qc_stopping: proc.kill(); proc.wait(); return None
                    rec=time.time()<self.recording_until
                    if rec and since is None:
                        if not self.cfg.get('qc_pause',True) or not _proc_suspend(proc,True):
                            if proc.poll() is not None: break       # 멈추려는 순간 이미 끝남
                            proc.kill(); proc.wait(); return None   # 예전 방식: 종료 후 처음부터
                        since=time.perf_counter(); self.qc_paused[key]['count']+=1
                        self.db.patch('qc',key,waiting_recording=True)
                        if self.qc_paused[key]['count']==1:
                            self.db.log(key,'녹화가 시작되어 QC 를 일시정지 · 녹화가 끝나면 이어서 실행',episode=key,station=q.get('station'))
                    elif not rec and since is not None:
                        if not _proc_suspend(proc,False): proc.kill(); proc.wait(); return None
                        self.qc_paused[key]['seconds']+=time.perf_counter()-since; since=None
                        self.db.patch('qc',key,waiting_recording=False)
                    time.sleep(0.2)
            finally: self.qc_procs.pop(key,None)
        if not out.is_file():
            tail=err.read_text(encoding='utf-8',errors='replace').strip().splitlines()[-3:] if err.is_file() else []
            return dict(ok=False,error='QC 프로세스가 결과 없이 끝났습니다 (코드 '+str(proc.returncode)+') '+' / '.join(tail))
        try: return json.loads(out.read_text(encoding='utf-8'))
        finally: out.unlink(missing_ok=True); err.unlink(missing_ok=True)
    def _qc_run(self,key):
        q=self.db.get('qc',key)
        if not q or q['status']!='queued': return
        out=self.root/'qc'/(key+'.json'); out.parent.mkdir(parents=True,exist_ok=True)
        try:
            while True:
                if self.qc_stopping: return
                waited=False
                while time.time()<self.recording_until:          # 녹화 우선: 녹화 중에는 시작하지 않는다
                    if not waited: self.db.patch('qc',key,status='queued',waiting_recording=True); waited=True
                    time.sleep(0.2)
                if waited: self.db.patch('qc',key,waiting_recording=False)
                attempts=(self.db.get('qc',key) or {}).get('attempts') or 0
                self.db.patch('qc',key,status='running',started_at=now(),attempts=attempts+1)
                t=time.perf_counter(); result=self._qc_once(key,q,out)
                if result is None:
                    if self.qc_stopping: self.db.patch('qc',key,status='queued'); return
                    self.db.log(key,'녹화가 시작되어 QC 를 멈춤 · 녹화가 끝나면 다시 실행',episode=key,station=q.get('station')); continue
                break
            paused=self.qc_paused.pop(key,None) or dict(seconds=0.0,count=0)
            seconds=round(time.perf_counter()-t-paused['seconds'],2)   # 일시정지한 시간은 뺀다 (실제로 돈 시간)
            self.db.patch('qc',key,waiting_recording=False,pauses=paused['count'],paused_seconds=round(paused['seconds'],2))
            if result.get('ok'):
                self.db.patch('qc',key,status='done',result=result,seconds=seconds,finished_at=now(),error=None)
                self.db.log(key,f"QC 완료 (export 서버): {result.get('overall')} · {seconds}초",episode=key,station=q.get('station'))
            else:
                self.db.patch('qc',key,status='failed',error=str(result.get('error','QC 실패')),seconds=seconds,finished_at=now())
                self.db.log(key,'QC 실패: '+str(result.get('error','')),'error',episode=key,station=q.get('station'))
        except Exception as e:
            self.db.patch('qc',key,status='failed',error=f'{type(e).__name__}: {e}')
            self.db.log(key,'QC 실패: '+str(e),'error',episode=key,station=q.get('station'))
    def stop_qc(self):
        """서버를 끌 때: 돌던 QC 프로세스를 멈춘다 (대기열은 다음에 켤 때 이어서)."""
        self.qc_stopping=True
        for p in list(self.qc_procs.values()):
            try: p.kill()
            except Exception: pass
        self.qc_pool.shutdown(wait=False,cancel_futures=True)
    # ── 같은 PC 지름길: 촬영 PC 원본 폴더를 바로 검증해 NAS 로 (ZIP · HTTP 전송 · 압축 풀기 없음) ──
    def local_begin(self,key,meta):
        safe_id(key); safe_id(meta['task_id']); safe_id(meta['episode_id']); safe_id(meta['station'])
        src=Path(meta['path'])
        if not src.is_absolute() or not src.is_dir() or src.name!=meta['episode_id']: raise ValueError('원본 폴더 경로 오류')
        with self.lock_for(key):
            j=self.db.get('job',key)
            if j:
                if j.get('mode')!='local' or j['episode_id']!=meta['episode_id']: raise ValueError('동일 작업 ID에 다른 데이터가 있습니다')
                if j['status']=='failed':
                    self.db.patch('job',key,status='queued',error=None,progress=0); self.pool.submit(self.process,key)
                return self.db.get('job',key)
            ep=validate_raw(src)                          # 승인 · 파일 개수 확인 (QC 와 별개인 전송 무결성 검사)
            if ep.get('id')!=meta['episode_id'] or ep.get('task_id')!=meta['task_id']: raise ValueError('에피소드 메타데이터와 전송 요청 불일치')
            j=self.db.put('job',key,dict(task_id=meta['task_id'],episode_id=meta['episode_id'],station=meta['station'],mode='local',
                                         source=str(src),status='queued',progress=0,created_at=now(),timings={}))
            self.log(key,'원본 폴더 접수 (같은 PC 지름길)')
            self.pool.submit(self.process,key)
            return j
    def lock_for(self,key):
        with self.lock: return self.locks.setdefault(key,threading.RLock())
    def upload_path(self,key): return self.root/'incoming'/(safe_id(key)+'.zip')
    def begin(self, key, meta):
        safe_id(key)
        if not 0<int(meta['size'])<=self.cfg['max_upload_bytes']: raise ValueError('파일 크기 초과')
        if not re.fullmatch('[0-9a-f]{64}',meta['sha256']): raise ValueError('SHA256 오류')
        safe_id(meta['task_id']); safe_id(meta['episode_id']); safe_id(meta['station'])
        with self.lock_for(key):
            j=self.db.get('job',key)
            if j:
                for k in ('size','sha256','task_id','episode_id','station'):
                    if j[k]!=meta[k]: raise ValueError('동일 작업 ID에 다른 데이터가 있습니다')
            else:
                j=self.db.put('job',key,dict(meta,status='receiving',progress=0,created_at=now(),timings={}))
                self.log(key,'원본 수신 시작')
            p=self.upload_path(key); p.parent.mkdir(parents=True,exist_ok=True)
            return dict(job=j,offset=p.stat().st_size if p.exists() else 0)
    def chunk(self,key,offset,data):
        with self.lock_for(key):
            j=self.db.get('job',key)
            if not j or j['status']!='receiving': raise ValueError('수신 중인 작업이 아닙니다')
            p=self.upload_path(key); size=p.stat().st_size if p.exists() else 0
            if offset!=size: return dict(offset=size)
            if not data or size+len(data)>j['size']: raise ValueError('청크 크기 오류')
            with open(p,'ab') as f: f.write(data); f.flush(); os.fsync(f.fileno())
            self.db.patch('job',key,progress=round((size+len(data))/j['size']*100,1))
            return dict(offset=size+len(data))
    def complete(self,key):
        with self.lock_for(key):
            j=self.db.get('job',key)
            if not j: raise ValueError('작업 없음')
            if j['status']!='receiving': return j
            p=self.upload_path(key)
            if not p.exists() or p.stat().st_size!=j['size']:
                raise ValueError('묶음 파일 수신이 끝나지 않았습니다')
            if digest(p)!=j['sha256']:
                p.unlink()
                self.db.patch('job',key,progress=0)
                self.log(key,'전송 체크섬 불일치. 다시 전송하면 처음부터 수신합니다.','error')
                raise ValueError('묶음 체크섬 불일치. 다시 전송하세요.')
            self.db.patch('job',key,status='queued',progress=0)
            self.pool.submit(self.process,key)
            return self.db.get('job',key)
    def retry(self,key):
        with self.lock_for(key):
            j=self.db.get('job',key)
            if not j or j['status']!='failed': raise ValueError('실패 작업만 재시도 가능합니다')
            self.db.patch('job',key,status='queued',error=None,progress=0)
            self.pool.submit(self.process,key)
    def stage(self,key,status,progress=0):
        self.db.patch('job',key,status=status,progress=progress)
        self.log(key,status)
    def _prepare_bundle(self,key,j,workspace):
        """ZIP 으로 받은 원본: 풀어서 파일별 체크섬 확인 (다른 PC 의 촬영 서버에서 보낸 경우)."""
        raw=workspace/'raw'
        ep=extract_verified(self.upload_path(key),raw,self.cfg['max_upload_bytes'])
        if not (raw/EPISODE_MARK).is_file():          # 표식을 쓰기 전에 찍은 에피소드는 여기서 만든다
            save_json(raw/EPISODE_MARK,episode_mark(ep))
        ext=json.loads((raw/'extrinsics.json').read_text(encoding='utf-8'))
        save_json(raw/'extrinsics.json',public_extrinsics(ext))
        for name in LOCAL_ONLY: (raw/name).unlink(missing_ok=True)
        return ep,{p.relative_to(raw).as_posix():p for p in sorted(raw.rglob('*')) if p.is_file()}
    def _prepare_local(self,key,j,workspace):
        """같은 PC 의 원본 폴더: 복사하지 않고 그대로 읽는다. 원본은 고치지 않고, 바꿔야 하는 작은 파일만 작업 폴더에 만든다."""
        src=Path(j['source'])
        if not src.is_dir(): raise ValueError('촬영 PC 원본 폴더가 없습니다: '+str(src))
        ep=validate_raw(src); gen=workspace/'gen'; files={}
        for p in sorted(src.rglob('*')):
            if not p.is_file(): continue
            if p.is_symlink(): raise ValueError('심볼릭 링크는 보낼 수 없습니다')
            rel=p.relative_to(src).as_posix()
            if rel in LOCAL_ONLY or rel.endswith('.tmp'): continue
            files[rel]=p
        if EPISODE_MARK not in files:
            save_json(gen/EPISODE_MARK,episode_mark(ep)); files[EPISODE_MARK]=gen/EPISODE_MARK
        ext=json.loads((src/'extrinsics.json').read_text(encoding='utf-8'))
        if set(ext)-set(EXTRINSICS_KEYS):
            save_json(gen/'extrinsics.json',public_extrinsics(ext)); files['extrinsics.json']=gen/'extrinsics.json'
        return ep,files
    def _parallel(self,fn,items):
        """파일 여러 개를 동시에 처리한다 (결과는 입력 순서). 파일마다 걸리는 지연(열기 · SMB 왕복)이 전송량보다 커서
        하나씩 하면 링크 속도의 1/3 도 못 쓴다. 동시 처리 수는 설정 nas_workers (1 이면 예전처럼 하나씩)."""
        n=max(1,int(self.cfg.get('nas_workers',4)))
        if n==1 or len(items)<2: return [fn(x) for x in items]
        pool=ThreadPoolExecutor(max_workers=n,thread_name_prefix='nas')
        try: return list(pool.map(fn,items))
        finally: pool.shutdown(wait=True,cancel_futures=True)   # 하나라도 실패하면 남은 것은 시작하지 않는다
    def process(self,key):
        start=time.perf_counter(); timing={}
        j=self.db.get('job',key); workspace=self.root/'work'/key; local=j.get('mode')=='local'
        try:
            self.stage(key,'extracting')
            if workspace.exists(): shutil.rmtree(workspace)
            self.wait_recording(key)
            ep,files=(self._prepare_local if local else self._prepare_bundle)(key,j,workspace)
            if ep['id']!=j['episode_id'] or ep['task_id']!=j['task_id'] or ep['station']!=j['station']:
                raise ValueError('에피소드 메타데이터와 전송 요청 불일치')
            if bool(ep.get('demo')) != bool(self.cfg['demo']): raise ValueError('데모/실장비 데이터 혼합 금지')
            # NAS 에는 PhysicalAI rgbd_raw 와 같은 녹화 원본 폴더를 변환 없이 그대로 올린다 (depth .npy 포함)
            # 원본 체크섬은 NAS 로 복사하면서 같이 계산한다 (copy_hashed) — 여기서 따로 읽지 않는다
            timing['extract_s']=round(time.perf_counter()-start,3)
            self.stage(key,'nas_copy'); t=time.perf_counter()
            if not nas_candidates(self.cfg): raise ValueError('export 서버의 nas_root를 설정하세요')
            nas=resolve_nas(self.cfg)
            if nas is None: raise ValueError('NAS 경로에 접근할 수 없습니다: '+' / '.join(nas_candidates(self.cfg)))
            if str(nas)!=str(Path(self.cfg.get('nas_root') or '')): self.log(key,'기본 NAS 경로에 접근할 수 없어 예비 경로로 저장: '+str(nas),'warning')
            # rgbd_raw 바로 아래에 ep_YYYYMMDD_HHMMSS (PhysicalAI 원본 폴더와 같은 이름 규칙) — 표식의 episodeId 와 항상 같게
            folder=str(json.loads(files[EPISODE_MARK].read_text(encoding='utf-8')).get('episodeId') or nas_folder_name(ep))
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}',folder) or folder in ('.','..'): raise ValueError('잘못된 에피소드 폴더 이름: '+folder)
            dest=nas/folder; temp=nas/(folder+'.'+key+'.part')
            rels=list(files)
            if dest.exists():
                # 같은 내용이면(재시도) 완료로 본다. 다른 내용이면 절대 덮어쓰지 않는다
                def local_entry(rel):
                    self.wait_recording(key)
                    return dict(path=rel,size=files[rel].stat().st_size,sha256=digest(files[rel]))
                def same(e):
                    self.wait_recording(key)
                    p=dest/e['path']
                    return p.is_file() and p.stat().st_size==e['size'] and digest(p)==e['sha256']
                entries=self._parallel(local_entry,rels)
                if not all(self._parallel(same,entries)):
                    raise ValueError('NAS에 같은 이름의 다른 에피소드 폴더가 있습니다. 덮어쓰지 않습니다: '+folder)
            else:
                if temp.exists(): shutil.rmtree(temp)
                temp.mkdir()
                for d in sorted({(temp/rel).parent for rel in rels}): d.mkdir(parents=True,exist_ok=True)
                state=dict(done=0,last=0.0); plock=threading.Lock()
                def copy_one(rel):
                    self.wait_recording(key)
                    size,sha=copy_hashed(files[rel],temp/rel)
                    with plock:
                        state['done']+=1
                        if time.time()-state['last']>0.5:
                            self.db.patch('job',key,progress=round(state['done']/max(len(rels),1)*100,1)); state['last']=time.time()
                    return dict(path=rel,size=size,sha256=sha)
                entries=self._parallel(copy_one,rels)
                self.stage(key,'verifying')
                def verify(e):
                    self.wait_recording(key)
                    p=temp/e['path']
                    if p.stat().st_size!=e['size'] or digest(p)!=e['sha256']: raise ValueError('NAS 체크섬 불일치: '+e['path'])
                self._parallel(verify,entries)
                temp.rename(dest)
            timing['nas_s']=round(time.perf_counter()-t,3); timing['total_s']=round(time.perf_counter()-start,3)
            # 중간 파일 정리 (받은 ZIP · 작업 폴더) — 완료 표시 전에 (완료를 본 쪽이 정리 전 상태를 보지 않게).
            # 같은 PC 원본은 촬영 서버가 NAS 완료를 확인한 뒤 지운다
            shutil.rmtree(workspace,ignore_errors=True)
            if not local:
                try: self.upload_path(key).unlink(missing_ok=True)
                except OSError: pass
            self.db.patch('job',key,status='done',progress=100,timings=timing,output=str(dest),error=None,
                          files=len(entries),bytes=sum(e['size'] for e in entries))
            self.log(key,'NAS 저장 및 체크섬 검증 완료')
        except Exception as e:
            self.db.patch('job',key,status='failed',error=str(e),timings=timing)
            self.log(key,str(e),'error')
