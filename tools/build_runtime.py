"""새 PC 용 Python 묶음 만들기 — 받는 PC 에 Python 설치 · 인터넷이 없어도 돌게 한다.
지금 패키지가 다 깔린 Python 으로 실행하면, 그 Python 의 본체(표준 라이브러리 · DLL)와
이 플랫폼이 쓰는 패키지(그리고 그 패키지가 필요로 하는 패키지)만 골라 dist/runtime/python 에 복사한다.
    <패키지가 깔린 python.exe> tools/build_runtime.py   →  dist/runtime/python
build_release.py 가 이 폴더를 zip 의 PhysicalAI_KAIST/python 으로 넣는다.
"""
from __future__ import annotations
import importlib.metadata as md
import shutil, subprocess, sys
from pathlib import Path
try:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ImportError:   # packaging 이 깨진 환경이면 pip 안에 든 것을 쓴다
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name

root=Path(__file__).resolve().parents[1]
out=root/'dist'/'runtime'/'python'
prefix=Path(sys.prefix)
site=prefix/'Lib'/'site-packages'
# 플랫폼이 직접 import 하는 것 (cv2 는 설치된 opencv 배포판 이름을 찾아 쓴다)
cv2_dists=md.packages_distributions().get('cv2') or []
if not cv2_dists:sys.exit('cv2(OpenCV) 가 이 Python 에 없습니다')
ROOTS=['fastapi','uvicorn','httpx','numpy','scipy','pyrealsense2',cv2_dists[0],'colorama']
# Python 본체에서 빼는 것 (패키지는 아래에서 골라 넣고, 나머지는 실행에 필요 없음)
SKIP_TOP={'lib/site-packages','scripts','conda-meta','include','libs','tools','share','etc','lib/test','lib/idlelib'}

def closure(names):
    seen={}; todo=list(names)
    while todo:
        n=canonicalize_name(todo.pop())
        if n in seen:continue
        try:d=md.distribution(n)
        except md.PackageNotFoundError:print('  (없음 — 건너뜀) '+n);continue
        seen[n]=d
        for r in d.requires or []:
            req=Requirement(r)
            if req.marker and not req.marker.evaluate({'extra':''}):continue
            todo.append(req.name)
    return seen

def copy_base():
    for p in prefix.rglob('*'):
        rel=p.relative_to(prefix); key=rel.as_posix().lower()
        if any(key==s or key.startswith(s+'/') for s in SKIP_TOP) or '__pycache__' in rel.parts or not p.is_file():continue
        dst=out/rel; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(p,dst)

def copy_dist(d):
    files=d.files
    if files:
        n=0
        for f in files:
            if f.parts[0]=='..' or '__pycache__' in f.parts:continue   # Scripts\*.exe 처럼 site-packages 밖은 뺀다
            src=Path(d.locate_file(f))
            if not src.is_file():continue
            dst=out/'Lib'/'site-packages'/f; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst); n+=1
        return n
    # RECORD 가 없는 배포판 (직접 깐 pyrealsense2 등): dist-info 와 최상위 모듈 폴더를 통째로
    info=Path(d._path); tops=(d.read_text('top_level.txt') or '').split() or [canonicalize_name(d.metadata['Name']).replace('-','_')]
    n=0
    for src in [info]+[site/t for t in tops]+[site/(t+'.py') for t in tops]:
        if src.is_dir():shutil.copytree(src,out/'Lib'/'site-packages'/src.name,ignore=shutil.ignore_patterns('__pycache__'),dirs_exist_ok=True);n+=1
        elif src.is_file():shutil.copy2(src,out/'Lib'/'site-packages'/src.name);n+=1
    return n

if out.exists():shutil.rmtree(out)
out.mkdir(parents=True)
print('원본 Python: '+str(prefix)+'  ('+sys.version.split()[0]+')')
copy_base()
dists=closure(ROOTS)
for name,d in sorted(dists.items()):
    print('  %-24s %-14s %d' % (name,d.version,copy_dist(d)))
(out/'Lib'/'site-packages').mkdir(parents=True,exist_ok=True)
(out/'PHYSICALAI_KAIST_RUNTIME.txt').write_text('PhysicalAI KAIST Platform 전용 Python ('+sys.version.split()[0]+')\n'+'\n'.join('%s==%s'%(n,d.version) for n,d in sorted(dists.items()))+'\n',encoding='utf-8')
size=sum(f.stat().st_size for f in out.rglob('*') if f.is_file())
print(str(out)+'  '+str(round(size/2**20))+' MB')
# 확인: 복사본만으로 필요한 모듈이 다 열리는지 (-I: 환경변수 · 사용자 site-packages 무시)
check="import fastapi,uvicorn,httpx,numpy,cv2,scipy.spatial,pyrealsense2,sqlite3,ssl; assert hasattr(cv2.aruco,'CharucoDetector'); print('ok', cv2.__version__, numpy.__version__)"
r=subprocess.run([str(out/'python.exe'),'-I','-c',check],capture_output=True,text=True,env={'SystemRoot':str(Path(sys.executable).drive)+'\\Windows','PATH':r'C:\Windows\System32'})
print('확인: '+(r.stdout.strip() or r.stderr.strip()[-800:]))
sys.exit(0 if r.returncode==0 else 1)
