"""관제 PC (관리자 노트북) 서버 — 이 PC 에서만 (127.0.0.1) 돈다.

촬영 PC 마다 도는 export 서버(:8766)에 서버 토큰으로 붙어 상태 · 작업 · NAS 저장 · 로그를 모아 보고,
작업(Task)을 만들고 · 끄고, 실패한 NAS 저장을 다시 시도한다. 데이터는 촬영 PC 에서 NAS 로 바로 가며 이 PC 를 거치지 않는다.
"""
from __future__ import annotations
import asyncio, hmac, secrets, time
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .core import Store, uid, now, safe_id
from . import update


class Login(BaseModel):
    password: str


class PC(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    url: str = Field(min_length=3, max_length=200)     # 예: 촬영PC이름 · 10.20.0.15 · http://촬영PC이름:8766
    token: str = Field(min_length=12, max_length=200)  # 촬영 PC production-config\export.json 의 transfer_token


class Task(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scene: str = '3D'
    target: int = Field(default=100, ge=1, le=1000000)
    prompt: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=100)
    object_name: str = Field(min_length=1, max_length=200)
    interaction: str = Field(min_length=1, max_length=500)


def normalize_url(u):
    u = u.strip().rstrip('/')
    if '://' not in u: u = 'http://' + u
    p = urlsplit(u)
    if p.scheme not in ('http', 'https') or not p.hostname: raise ValueError('주소 형식 오류')
    return f'{p.scheme}://{p.hostname}:{p.port or 8766}'


def create_control_app(cfg):
    db = Store(cfg['data_dir']); sessions = {}; attempts = {}
    COOKIE = 'studio_session_' + str(cfg['port'])
    app = FastAPI(title='PhysicalAI KAIST Platform Control')

    @app.middleware('http')
    async def guard(request, call_next):
        host = request.client.host if request.client else ''
        if host not in ('127.0.0.1', '::1', 'testclient'):
            return JSONResponse({'detail': '관제 화면은 이 PC 에서만 열 수 있습니다'}, 403)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if origin and urlsplit(origin).netloc != request.headers.get('host'):
                return JSONResponse({'detail': '다른 사이트의 요청 차단'}, 403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'; response.headers['X-Frame-Options'] = 'DENY'
        if request.url.path.startswith('/api'): response.headers['Cache-Control'] = 'no-store'
        else: response.headers['Cache-Control'] = 'no-cache'
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc): return JSONResponse({'detail': str(exc)}, 400)

    async def auth(request: Request):
        s = sessions.get(request.cookies.get(COOKIE))
        if not s or s['expires'] < time.time(): raise HTTPException(401, '로그인이 필요합니다')

    @app.get('/api/info')
    def info(): return dict(role='control')

    @app.post('/api/machine/shutdown')
    def shutdown(request: Request):
        """끄기 스크립트용 (이 PC · 관제 비밀번호)."""
        if not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + cfg['admin_password']): raise HTTPException(403)
        import os, threading
        threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
        return dict(ok=True)

    @app.post('/api/login')
    def login(body: Login, request: Request, response: Response):
        a = attempts.get('local', [0, 0])
        if a[0] >= 10 and time.time() - a[1] < 60: raise HTTPException(429, '잠시 후 다시 시도하세요')
        if not hmac.compare_digest(body.password, cfg['admin_password']):
            attempts['local'] = [a[0] + 1, time.time()]; raise HTTPException(401, '비밀번호를 확인하세요')
        attempts.pop('local', None); token = secrets.token_urlsafe(32)
        sessions[token] = dict(expires=time.time() + 12 * 3600)
        response.set_cookie(COOKIE, token, httponly=True, samesite='strict', max_age=12 * 3600)
        return dict(ok=True)

    @app.post('/api/logout')
    def logout(request: Request, response: Response):
        sessions.pop(request.cookies.get(COOKIE), None); response.delete_cookie(COOKIE); return dict(ok=True)

    def pc_of(pid):
        pc = db.get('pc', safe_id(pid))
        if not pc: raise HTTPException(404, '등록되지 않은 촬영 PC')
        return pc

    def call(pc, method, path, timeout=5, **kw):
        try:
            with httpx.Client(base_url=pc['url'], headers={'Authorization': 'Bearer ' + pc['token']}, timeout=timeout, trust_env=False) as c:
                r = c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"{pc['name']} 에 연결할 수 없습니다 ({pc['url']}): {type(e).__name__}")
        if r.status_code >= 400:
            try: detail = r.json().get('detail')
            except Exception: detail = r.text[:200]
            raise HTTPException(502 if r.status_code >= 500 else r.status_code, f"{pc['name']}: {detail}")
        return r.json()

    def public(pc): return {k: pc.get(k) for k in ('id', 'name', 'url', 'created_at')}

    @app.get('/api/pcs', dependencies=[Depends(auth)])
    def pcs(): return [public(p) for p in db.all('pc')]

    @app.post('/api/pcs', dependencies=[Depends(auth)])
    def add_pc(body: PC):
        pc = dict(name=body.name.strip(), url=normalize_url(body.url), token=body.token.strip())
        call(pc, 'GET', '/api/machine/overview')          # 등록 전에 실제로 붙는지 · 토큰이 맞는지 확인
        same = next((p for p in db.all('pc') if p['url'] == pc['url']), None)
        key = same['id'] if same else uid('pc_')
        db.put('pc', key, dict(pc, created_at=(same or {}).get('created_at') or now()))
        return public(db.get('pc', key))

    @app.delete('/api/pcs/{pid}', dependencies=[Depends(auth)])
    def remove_pc(pid: str):
        pc_of(pid)
        with db.connect() as c: c.execute("DELETE FROM records WHERE kind='pc' AND id=?", (pid,))
        return dict(ok=True)

    @app.get('/api/overview', dependencies=[Depends(auth)])
    async def overview():
        async def one(pc):
            t = time.perf_counter()
            try:
                data = await asyncio.to_thread(call, pc, 'GET', '/api/machine/overview', 4)
                return dict(pc=public(pc), online=True, ms=round((time.perf_counter() - t) * 1000), data=data)
            except HTTPException as e:
                return dict(pc=public(pc), online=False, error=e.detail)
        local = await asyncio.to_thread(update.manifest, source_root)
        return dict(at=now(), version=local['version'], files=len(local['files']), pcs=await asyncio.gather(*[one(p) for p in db.all('pc')]))

    source_root = Path(cfg.get('install_root') or update.ROOT)   # 이 PC 의 코드 (여기서 고친 코드가 촬영 PC 로 간다)

    @app.post('/api/pcs/{pid}/update', dependencies=[Depends(auth)])
    def push_update(pid: str):
        """이 PC 의 지금 코드를 촬영 PC 로 보낸다. 촬영 PC 가 검증 → 서버 재시작 → 새 버전 확인 (실패하면 스스로 되돌림)."""
        pc = pc_of(pid)
        m, data = update.make_bundle(source_root)
        r = call(pc, 'POST', '/api/machine/update', timeout=60, content=data, headers={'Content-Type': 'application/zip'})
        db.log(pc['id'], f"업데이트 보냄: {pc['name']} → {m['version']}" + (' (이미 최신)' if r.get('already') else ''))
        return dict(r, name=pc['name'])

    @app.post('/api/pcs/{pid}/tasks', dependencies=[Depends(auth)])
    def create_task(pid: str, body: Task): return call(pc_of(pid), 'POST', '/api/machine/tasks', json=body.model_dump())

    @app.post('/api/pcs/{pid}/tasks/{tid}/toggle', dependencies=[Depends(auth)])
    def toggle(pid: str, tid: str): return call(pc_of(pid), 'POST', f'/api/machine/tasks/{safe_id(tid)}/toggle')

    @app.post('/api/pcs/{pid}/jobs/{jid}/retry', dependencies=[Depends(auth)])
    def retry(pid: str, jid: str): return call(pc_of(pid), 'POST', f'/api/machine/jobs/{safe_id(jid)}/retry')

    @app.get('/api/pcs/{pid}/logs', dependencies=[Depends(auth)])
    def logs(pid: str, episode: str | None = None, q: str | None = None, level: str | None = None, station: str | None = None, limit: int = 300):
        params = {k: v for k, v in dict(episode=episode, q=q, level=level, station=station, limit=limit).items() if v}
        return call(pc_of(pid), 'GET', '/api/machine/logs', params=params)

    web = Path(__file__).parent / 'web'
    app.mount('/static', StaticFiles(directory=web), name='static')

    @app.get('/')
    @app.get('/control')
    def home(): return FileResponse(web / 'control.html')

    return app
