r"""원격 업데이트 — 관제 PC(이 노트북)의 코드를 촬영 PC 로 보내 바꿔 끼운다.

- 대상: studio · tools · tests 폴더, 최상위 *.cmd · README 등 (소스만). 전용 Python(python\) · 설정 · 원본 · DB 는 건드리지 않는다.
- 버전: 대상 파일들의 SHA256 을 합친 값 앞 12자리. 같은 코드면 어느 PC 든 같은 버전.
- 촬영 PC: 받은 묶음을 검증해 updates\staging 에 풀고, 새 tools\Apply-Update.ps1 을 따로 띄운다.
  그 스크립트가 서버를 정상 종료 → 지금 코드를 updates\backup 에 보관 → 새 코드로 교체 → 서버 켜기 → 새 버전이 뜨는지 확인,
  안 뜨면 보관해 둔 코드로 되돌리고 다시 켠다. 결과는 updates\last-update.json · logs\update.log.
"""
from __future__ import annotations
import hashlib, io, json, re, shutil, subprocess, time, zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DIRS = ('studio', 'tools', 'tests')
TOP_FILES = ('README.md', 'requirements.txt', 'requirements-dev.txt', 'SOURCE_PROVENANCE.json', 'TEST_REPORT.md', '.gitignore')
SKIP_SUFFIX = {'.pyc', '.pyo', '.log', '.prev', '.old', '.zip', '.part'}
REQUIRED = ('studio/app.py', 'studio/update.py', 'tools/Server.ps1', 'tools/Apply-Update.ps1')
MAX_BYTES = 50 * 2**20
IN_PROGRESS = ('staged', 'stopping', 'applying', 'starting', 'rolling_back')


def _top_ok(name): return name in TOP_FILES or name.lower().endswith('.cmd')


def source_files(root=ROOT):
    root = Path(root); out = set()
    for d in DIRS:
        base = root / d
        if not base.is_dir(): continue
        for p in base.rglob('*'):
            rel = p.relative_to(root)
            if p.is_file() and '__pycache__' not in rel.parts and p.suffix.lower() not in SKIP_SUFFIX: out.add(rel.as_posix())
    for p in root.iterdir():
        if p.is_file() and _top_ok(p.name): out.add(p.name)
    return sorted(out)


def version_of(files):
    h = hashlib.sha256()
    for rel in sorted(files): h.update((rel + '\0' + files[rel] + '\n').encode())
    return h.hexdigest()[:12]


def manifest(root=ROOT):
    root = Path(root)
    files = {rel: hashlib.sha256((root / rel).read_bytes()).hexdigest() for rel in source_files(root)}
    return dict(version=version_of(files), files=files)


def version(root=ROOT): return manifest(root)['version']


def make_bundle(root=ROOT):
    """관제 PC: 지금 이 PC 의 코드를 묶는다 (수백 KB)."""
    m = manifest(root); buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json', json.dumps(dict(m, built_at=datetime.now(timezone.utc).isoformat()), ensure_ascii=False))
        for rel in m['files']: z.write(Path(root) / rel, 'files/' + rel)
    return m, buf.getvalue()


def safe_rel(rel):
    if not isinstance(rel, str) or '\\' in rel or ':' in rel: raise ValueError('잘못된 파일 경로: ' + str(rel))
    p = PurePosixPath(rel)
    if p.is_absolute() or not p.parts or '..' in p.parts or '__pycache__' in p.parts: raise ValueError('잘못된 파일 경로: ' + rel)
    if not ((p.parts[0] in DIRS and len(p.parts) > 1) or (len(p.parts) == 1 and _top_ok(p.name))): raise ValueError('업데이트 대상이 아닌 경로: ' + rel)
    return p


def last_status(root=ROOT):
    f = Path(root) / 'updates' / 'last-update.json'
    try: return json.loads(f.read_text(encoding='utf-8-sig'))
    except Exception: return None


def busy_updating(root=ROOT):
    s = last_status(root)
    if not s or s.get('status') not in IN_PROGRESS: return False
    try: age = time.time() - datetime.fromisoformat(str(s.get('at')).replace('Z', '+00:00')).timestamp()
    except Exception: age = 0
    return age < 600    # 10분 넘게 멈춘 기록은 무시 (스크립트가 죽은 경우)


def _write_status(root, **kw):
    upd = Path(root) / 'updates'; upd.mkdir(exist_ok=True)
    (upd / 'last-update.json').write_text(json.dumps(dict(kw, at=datetime.now(timezone.utc).isoformat()), ensure_ascii=False, indent=1), encoding='utf-8')


def stage(root, data, current):
    """촬영 PC: 받은 묶음을 검증해 updates\\staging 에 푼다. 파일 하나라도 해시가 다르면 아무것도 바꾸지 않는다."""
    root = Path(root)
    if len(data) > MAX_BYTES: raise ValueError('업데이트 묶음이 너무 큽니다')
    try: z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile: raise ValueError('업데이트 묶음이 zip 이 아닙니다')
    with z:
        if sum(i.file_size for i in z.infolist()) > MAX_BYTES: raise ValueError('업데이트 묶음이 너무 큽니다')
        m = json.loads(z.read('manifest.json'))
        ver, files = m.get('version'), m.get('files')
        if not isinstance(ver, str) or not re.fullmatch(r'[0-9a-f]{12}', ver) or not isinstance(files, dict) or not files: raise ValueError('업데이트 목록이 잘못되었습니다')
        missing = [r for r in REQUIRED if r not in files]
        if missing: raise ValueError('필수 파일이 빠졌습니다: ' + ', '.join(missing))
        if version_of(files) != ver: raise ValueError('업데이트 버전 값이 파일 목록과 다릅니다')
        staging = root / 'updates' / 'staging'
        if staging.exists(): shutil.rmtree(staging)
        for rel, sha in files.items():
            p = safe_rel(rel)
            try: b = z.read('files/' + rel)
            except KeyError: raise ValueError('묶음에 파일이 없습니다: ' + rel)
            if hashlib.sha256(b).hexdigest() != sha: raise ValueError('파일이 손상되었습니다: ' + rel)
            dst = staging / 'files' / Path(*p.parts); dst.parent.mkdir(parents=True, exist_ok=True); dst.write_bytes(b)
    # Apply-Update.ps1 이 읽는 목록: 새 파일 · 지금 파일 (지금 파일은 보관용 · 새 목록에 없으면 지운다)
    (staging / 'new-files.txt').write_text('\n'.join(sorted(files)) + '\n', encoding='utf-8')
    (staging / 'current-files.txt').write_text('\n'.join(source_files(root)) + '\n', encoding='utf-8')
    _write_status(root, status='staged', version=ver, **{'from': current}, message='업데이트 파일을 받았습니다 · 서버 재시작 준비')
    return dict(version=ver, files=len(files))


def launch_apply(root, ver):
    """새 코드의 Apply-Update.ps1 을 이 서버와 상관없는 독립 프로세스로 띄운다 (서버가 꺼져도 계속 돈다)."""
    root = Path(root); upd = root / 'updates'
    runner = upd / ('apply-' + ver + '.ps1')
    shutil.copy2(upd / 'staging' / 'files' / 'tools' / 'Apply-Update.ps1', runner)
    for old in upd.glob('apply-*.ps1'):
        if old != runner:
            try: old.unlink()
            except OSError: pass
    q = lambda s: str(s).replace("'", "''")
    args = f'-NoProfile -ExecutionPolicy Bypass -File "{q(runner)}" -Root "{q(root)}" -Version {ver}'
    # 한 번 더 띄워서(중간 프로세스는 바로 끝남) 서버 프로세스와 부모 · 자식 관계를 끊는다
    inner = f"Start-Process powershell.exe -WindowStyle Hidden -WorkingDirectory '{q(root)}' -ArgumentList '{q(args)}'"
    subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', inner], cwd=str(root),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)
