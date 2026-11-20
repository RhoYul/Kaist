"""새 PC 로 옮길 패키지 만들기 — 소스 · 실행 · 설치 파일과 전용 Python 을 한 zip 에 담는다.
받는 PC 에는 Python 설치도 인터넷도 필요 없다. zip 을 풀고 Setup-CapturePC.cmd / Setup-ControlPC.cmd 만 실행한다.
설정(비밀번호 · 토큰) · 원본 · 로그는 넣지 않는다.
    <패키지가 깔린 python.exe> tools/build_runtime.py     →  dist/runtime/python   (전용 Python, 패키지가 바뀔 때만)
    python tools/build_release.py                         →  dist/PhysicalAI_KAIST-YYYYMMDD.zip
    python tools/build_release.py --no-python             →  dist/PhysicalAI_KAIST-YYYYMMDD-src.zip  (소스만 · 받는 PC 에서 인터넷 설치)
"""
from pathlib import Path
import sys, time, zipfile
root=Path(__file__).resolve().parents[1]
runtime=root/'dist'/'runtime'/'python'
with_python='--no-python' not in sys.argv
if with_python and not (runtime/'python.exe').exists():
    sys.exit('전용 Python 이 없습니다. 먼저 실행: <패키지가 깔린 python.exe> tools/build_runtime.py   (소스만 담으려면 --no-python)')
out=root/'dist'/('PhysicalAI_KAIST-'+time.strftime('%Y%m%d')+('' if with_python else '-src')+'.zip'); out.parent.mkdir(exist_ok=True)
skip_dirs={'runtime','runtime-real','runtime-control','config','production-config','control-config','logs','dist','.venv','python','updates','__pycache__','.git','.claude'}
files=[]
for p in root.rglob('*'):
    rel=p.relative_to(root)
    if not p.is_file() or any(x in skip_dirs for x in rel.parts):continue
    if p.suffix in {'.pyc','.png','.mp4','.npy','.sqlite3','.log','.prev','.old','.zip'}:continue
    files.append((p,Path('PhysicalAI_KAIST')/rel))
src_count=len(files)
if with_python:
    files+=[(p,Path('PhysicalAI_KAIST')/'python'/p.relative_to(runtime)) for p in runtime.rglob('*') if p.is_file()]
tmp=out.with_suffix('.zip.part')
with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for p,arc in sorted(files,key=lambda x:str(x[1])):z.write(p,arc)
tmp.replace(out)
print(out)
print('%d files (source %d%s); %.1f MB' % (len(files),src_count,', python %d' % (len(files)-src_count) if with_python else '',out.stat().st_size/2**20))
