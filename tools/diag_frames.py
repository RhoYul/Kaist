r"""촬영 프레임 누락 원인 진단 (촬영 PC 전용 · 촬영 서버를 끈 상태에서 실행 — Diag-Frames.cmd 가 끄고 다시 켠다).

카메라가 만든 프레임은 하드웨어 시각(hw timestamp)이 33.3ms(30fps) 간격으로 붙는다. 간격이 그보다 크면 그 사이 프레임이
PC 까지 오지 못한 것 → '누락'. 같은 카메라 4대로 조건만 바꿔 누락이 어디서 생기는지 가른다.

  A  4대 동시 · 받기만         (정렬 · 저장 없음)          → 여기서 빠지면 USB · 카메라 · 드라이버 쪽
  D  1대씩 따로 · 받기만                                   → A 에서 빠지고 D 에서 안 빠지면 대역폭(동시 연결) 문제
  B  4대 동시 · keep + 정렬     (저장 없음)                 → A 는 괜찮고 B 에서 빠지면 정렬 · 프레임 버퍼 문제
  L  B + 파이썬 부하 · 예전 방식 (wait_for_frames)          → B 는 괜찮고 L 에서 빠지면 촬영 서버가 바쁠 때 늦게 꺼내서 버려지는 것
  Q  B + 파이썬 부하 · SDK 버퍼 (rs.frame_queue)            → L 에서 빠지고 Q 에서 안 빠지면 SDK 버퍼로 해결됨 (warm.SDK_QUEUE)
  C  4대 동시 · 실제 녹화 경로   (warm.WarmSession 그대로)  → B 는 괜찮고 C 에서 빠지면 저장(mp4 · npy) 부하 문제

결과는 화면과 logs\diag-frames-<시각>.txt 에 남는다. 그 파일을 관제 PC 로 보내면 된다.
사용:  python tools\diag_frames.py [--seconds 10] [--only A,D,B,C] [--config production-config\capture.json]
"""
from __future__ import annotations
import argparse, json, os, platform, queue, shutil, sys, threading, time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

from studio.vendor import realsense as rv   # noqa: E402
from studio import warm                     # noqa: E402

WARMUP_S = 2.0      # 연 뒤 자동 노출이 자리 잡을 때까지 버리는 시간
LINES: list[str] = []


def out(s=''):
    print(s, flush=True); LINES.append(s)


class Gaps:
    """한 스트림의 hw 시각으로 누락 수를 센다."""
    def __init__(self, fps):
        self.slot = 1000.0 / fps; self.last = None; self.n = 0; self.missing = 0; self.events = 0; self.max_gap = 0.0

    def add(self, hw):
        if self.last is not None:
            g = hw - self.last
            k = int(round(g / self.slot)) - 1
            if k > 0: self.missing += k; self.events += 1
            self.max_gap = max(self.max_gap, g)
        self.last = hw; self.n += 1

    def pct(self): return 100.0 * self.missing / max(1, self.n + self.missing)


def open_pipe(rs, serial, fq=None):
    p = rs.pipeline(); c = rs.config(); c.enable_device(serial)
    c.enable_stream(rs.stream.color, rv.COLOR_W, rv.COLOR_H, rs.format.bgr8, rv.COLOR_FPS)
    c.enable_stream(rs.stream.depth, rv.DEPTH_W, rv.DEPTH_H, rs.format.z16, rv.DEPTH_FPS)
    if fq is None: p.start(c)
    else:
        prof = p.start(c, fq)        # SDK 가 자기 스레드로 버퍼에 쌓는다 (촬영 서버 warm.py 와 같은 방식)
        try:
            for sen in prof.get_device().query_sensors():
                if sen.supports(rs.option.frames_queue_size):
                    sen.set_option(rs.option.frames_queue_size, sen.get_option_range(rs.option.frames_queue_size).max)
        except Exception: pass
    return p


def busy_load(stop, threads=3):
    """촬영 서버의 화면 응답 · 상태 정리처럼 파이썬이 계속 바쁜 상황을 흉내 낸다 (스레드 여러 개가 JSON 을 만들고 읽는다)."""
    # 부하는 '가끔 수십 ms 늦게 꺼내는' 정도로만 건다. 쉬지 않고 돌리면 정렬 자체가 프레임 간격보다 느려져
    # (2026-11-19 실측: 정렬 평균 120ms · 처리 큐 100장 이상) 어떤 방식으로 받아도 빠지므로 비교가 되지 않는다
    blob = {f'ep_{i}': dict(id=i, name='x' * 40, values=list(range(60))) for i in range(60)}
    def loop():
        while not stop.is_set():
            t = time.perf_counter()
            while time.perf_counter() - t < 0.04: json.loads(json.dumps(blob))
            time.sleep(0.12)
    ts = [threading.Thread(target=loop, daemon=True) for _ in range(threads)]
    for t in ts: t.start()
    return ts


def run_stream_test(serials, seconds, mode, load=False, sdk_queue=0):
    """mode 'recv' = 받기만 / 'align' = 실제 녹화처럼 카메라 스레드는 keep 후 큐에 넣고, 처리 스레드가 정렬 · 복사.
    load = 파이썬 부하를 함께 건다 / sdk_queue = SDK 버퍼(장)로 받는다 (0 이면 wait_for_frames)."""
    import numpy as np
    rs = rv._rs()
    pipes = {}; fqs = {}
    for s in serials:               # 서버처럼 하나씩 연다
        fqs[s] = rs.frame_queue(int(sdk_queue), True) if sdk_queue else None
        pipes[s] = open_pipe(rs, s, fqs[s])
    res = {s: dict(color=Gaps(rv.COLOR_FPS), depth=Gaps(rv.DEPTH_FPS), wait_max=0.0, proc_ms=[], qmax=0, timeouts=0) for s in serials}
    t_start = time.time() + WARMUP_S; t_end = t_start + seconds
    stop = threading.Event()

    def cam_loop(s):
        p = pipes[s]; r = res[s]; align = rs.align(rs.stream.color) if mode == 'align' else None
        q: "queue.Queue" = queue.Queue(maxsize=rv.COLOR_FPS * (seconds + 4))
        worker = None
        if mode == 'align':
            def proc():
                while True:
                    item = q.get()
                    if item is None: break
                    t0 = time.perf_counter()
                    fs = align.process(item)
                    c = fs.get_color_frame(); d = fs.get_depth_frame()
                    if c and d:
                        np.asanyarray(c.get_data()).copy(); np.asanyarray(d.get_data()).copy()
                    r['proc_ms'].append((time.perf_counter() - t0) * 1000)
            worker = threading.Thread(target=proc, daemon=True); worker.start()
        last_ret = None
        fq = fqs[s]
        while not stop.is_set():
            try:
                if fq is None: fs = p.wait_for_frames(timeout_ms=1000)
                else:
                    f = fq.wait_for_frame(1000)
                    if not f.is_frameset(): r['loose'] = r.get('loose', 0) + 1; continue
                    fs = f.as_frameset()
            except RuntimeError: r['timeouts'] += 1; continue
            now = time.time()
            if now < t_start: last_ret = now; continue
            if now >= t_end: break
            if last_ret is not None: r['wait_max'] = max(r['wait_max'], now - last_ret)
            last_ret = now
            c = fs.get_color_frame(); d = fs.get_depth_frame()
            if c: r['color'].add(c.get_timestamp())
            if d: r['depth'].add(d.get_timestamp())
            if mode == 'align':
                fs.keep()
                try: q.put_nowait(fs)
                except queue.Full: pass
                r['qmax'] = max(r['qmax'], q.qsize())
        if worker:
            q.put(None); worker.join(30)

    cpu = cpu_sampler(seconds)
    th = [threading.Thread(target=cam_loop, args=(s,), daemon=True) for s in serials]
    for t in th: t.start()
    if load: busy_load(stop)
    for t in th: t.join(seconds + WARMUP_S + 40)
    stop.set()
    for p in pipes.values():
        try: p.stop()
        except Exception: pass
    time.sleep(1.5)                  # 장치가 풀릴 시간
    return res, cpu()


def run_real_path(serials, seconds, data_dir):
    """서버가 쓰는 warm 모듈 그대로: 카메라 서비스 → WarmSession.start → wait_first → schedule_stop → stop."""
    svc = warm.WarmCameraService.get()
    deadline = time.time() + 20
    while time.time() < deadline and not (len(svc._cameras) >= len(serials) and all(c.get_status()['connected'] for c in svc._cameras.values())):
        time.sleep(0.2)
    time.sleep(WARMUP_S)
    tmp = Path(data_dir) / '_diag_frames' / datetime.now().strftime('%Y%m%d_%H%M%S')
    sess = warm.WarmSession(svc)
    cpu = cpu_sampler(seconds)
    try:
        sess.start(tmp, seconds=seconds)
        first = sess.wait_first(3.0); until = sess.schedule_stop(seconds)
        while time.time() < until + 0.1: time.sleep(0.02)
        result = sess.stop()
        cpu_s = cpu()
        rows = {}
        for name, r in result.items():
            if 'error' in r: rows[name] = dict(error=r['error']); continue
            g = Gaps(rv.COLOR_FPS)
            for line in Path(r['timestamps_path']).read_text().splitlines()[1:]:
                g.add(float(line.split(',')[1]))
            rows[name] = dict(saved=g.n, missing=g.missing, events=g.events, max_gap=g.max_gap, pct=g.pct(), dropped=r.get('dropped', 0))
        return rows, cpu_s
    finally:
        try: svc.shutdown()
        except Exception: pass
        warm.WarmCameraService._instance = None
        shutil.rmtree(tmp.parent, ignore_errors=True)
        time.sleep(1.5)


def hold_nas(cfg):
    """진단하는 동안 export 서버에 '녹화 중'이라고 알려 NAS 복사 · QC 를 멈춰 둔다 (실제 녹화 때와 같은 조건).
    촬영 서버가 꺼져 있으면 export 서버는 쉬지 않고 NAS 로 복사하는데, NAS 랜 어댑터가 카메라와 같은 USB 허브에 있어
    그 통신이 진단 결과에 섞인다 (2026-11-19 진단이 그랬다). 알림은 20초 뒤 저절로 풀리므로 5초마다 다시 보낸다."""
    import urllib.request
    url, token = str(cfg.get('export_url') or '').rstrip('/'), cfg.get('transfer_token')
    if not url or not token: out('  (export 서버 설정이 없어 NAS 복사를 멈추지 못함 — 결과에 NAS 통신이 섞일 수 있음)'); return
    def post():
        req = urllib.request.Request(url + '/api/machine/recording', data=b'{"active": true}', method='POST',
                                     headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=2).read()
    try: post(); out('  진단 동안 export 서버의 NAS 복사 · QC 를 멈춰 둠 (실제 녹화와 같은 조건)')
    except Exception as e: out(f'  (export 서버에 알리지 못함: {e} — 결과에 NAS 통신이 섞일 수 있음)'); return
    def loop():
        while True:
            time.sleep(5)
            try: post()
            except Exception: pass
    threading.Thread(target=loop, daemon=True).start()


def cpu_sampler(seconds):
    """psutil 이 있으면 테스트 동안 CPU 사용률 (평균 · 최고 코어)."""
    try: import psutil
    except ImportError: return lambda: None
    samples = []; stop = threading.Event()
    def loop():
        psutil.cpu_percent(percpu=True)
        while not stop.wait(0.5): samples.append(psutil.cpu_percent(percpu=True))
    t = threading.Thread(target=loop, daemon=True); t.start()
    def done():
        stop.set(); t.join(2)
        if not samples: return None
        avg = sum(sum(s) / len(s) for s in samples) / len(samples)
        return dict(avg=round(avg), max_core=round(max(max(s) for s in samples)))
    return done


def fmt_cpu(c): return '' if not c else f"  · CPU 평균 {c['avg']}% · 가장 바쁜 코어 {c['max_core']}%"


def report_stream(title, res, names, cpu, mode):
    out(f'\n[{title}]' + fmt_cpu(cpu))
    out('  카메라   시리얼          컬러 받음  누락(%)        끊김  최대간격  | depth 누락(%)' + ('  | 정렬 ms 평균/최고  큐 최고' if mode == 'align' else ''))
    tot_m = tot_n = 0
    for s, r in res.items():
        c, d = r['color'], r['depth']; tot_m += c.missing; tot_n += c.n + c.missing
        line = f"  {names[s]:7s} {s:15s} {c.n:6d}   {c.missing:4d} ({c.pct():4.1f}%)  {c.events:4d}  {c.max_gap:6.0f}ms | {d.missing:4d} ({d.pct():4.1f}%)"
        if mode == 'align' and r['proc_ms']:
            pm = r['proc_ms']; line += f"  | {sum(pm)/len(pm):5.1f} / {max(pm):5.1f}   {r['qmax']:4d}"
        if r['timeouts']: line += f"  · 프레임 대기 시간초과 {r['timeouts']}회"
        if r.get('loose'): line += f"  · 묶음이 아닌 프레임 {r['loose']}장 (SDK 버퍼 방식 불가)"
        out(line)
    pct = 100.0 * tot_m / max(1, tot_n)
    out(f'  → 전체 컬러 누락 {tot_m}장 ({pct:.1f}%)')
    return pct


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=int, default=10)
    ap.add_argument('--only', default='A,D,B,L,Q,C')
    ap.add_argument('--config', default=str(ROOT / 'production-config' / 'capture.json'))
    a = ap.parse_args()
    cfg = {}
    try: cfg = json.loads(Path(a.config).read_text(encoding='utf-8-sig'))
    except Exception as e: print('설정 파일을 읽지 못해 기본값(848x480@30)으로 진행:', e)
    W, H, F = int(cfg.get('capture_width', 848)), int(cfg.get('capture_height', 480)), int(cfg.get('capture_fps', 30))
    warm.configure(W, H, F)
    data_dir = Path(cfg.get('data_dir') or (ROOT / 'data'))
    if not data_dir.is_absolute(): data_dir = (ROOT / data_dir).resolve()   # 서버는 설치 폴더에서 돌므로 같은 기준
    tests = [t.strip().upper() for t in a.only.split(',') if t.strip()]

    out('=' * 78)
    out(f'프레임 누락 진단 · {datetime.now():%Y-%m-%d %H:%M:%S} · {platform.node()} · {W}x{H}@{F} · 테스트당 {a.seconds}초')
    out('=' * 78)
    try: rs = rv._rs()
    except RuntimeError as e: out(str(e)); return 2
    out(f'pyrealsense2 {getattr(rs, "__version__", "?")} · CPU 논리코어 {os.cpu_count()}')
    serials = rv.discover_devices()
    if not serials: out('RealSense 카메라를 찾지 못했습니다. 촬영 서버가 켜져 있으면 끄고 다시 실행하세요.'); return 2
    info = warm._device_info()
    names = {s: f'cam_{i + 1}' for i, s in enumerate(serials)}
    for s in serials:
        i = info.get(s, {}); u = i.get('usb', '?')
        out(f"  {names[s]}  {s}  {i.get('model', '?')}  USB {u}" + ('   ← USB 2 연결! USB 3 포트 · 케이블로 바꾸세요' if str(u).startswith('2') else ''))
    hold_nas(cfg)
    summary = {}
    if 'A' in tests:
        res, cpu = run_stream_test(serials, a.seconds, 'recv')
        summary['A'] = report_stream('A · 4대 동시 · 받기만', res, names, cpu, 'recv')
    if 'D' in tests:
        out('\n[D · 1대씩 따로 · 받기만]')
        tot = []
        for s in serials:
            res, cpu = run_stream_test([s], a.seconds, 'recv')
            c = res[s]['color']; tot.append(c.pct())
            out(f"  {names[s]:7s} {s:15s} 받음 {c.n:5d} · 누락 {c.missing:4d} ({c.pct():4.1f}%) · 최대간격 {c.max_gap:5.0f}ms" + fmt_cpu(cpu))
        summary['D'] = max(tot) if tot else 0.0
    if 'B' in tests:
        res, cpu = run_stream_test(serials, a.seconds, 'align')
        summary['B'] = report_stream('B · 4대 동시 · keep + 정렬 (저장 없음)', res, names, cpu, 'align')
    if 'L' in tests:
        res, cpu = run_stream_test(serials, a.seconds, 'align', load=True)
        summary['L'] = report_stream('L · B + 파이썬 부하 (촬영 서버가 바쁜 상황) · 예전 방식 wait_for_frames', res, names, cpu, 'align')
    if 'Q' in tests:
        res, cpu = run_stream_test(serials, a.seconds, 'align', load=True, sdk_queue=warm.SDK_QUEUE or 32)
        summary['Q'] = report_stream(f'Q · B + 파이썬 부하 · SDK 버퍼 {warm.SDK_QUEUE or 32}장', res, names, cpu, 'align')
    if 'C' in tests:
        out(f'\n[C · 실제 녹화 경로 (warm.WarmSession) · {a.seconds}초 · 임시 폴더에 저장 후 삭제]')
        try:
            rows, cpu = run_real_path(serials, a.seconds, data_dir)
            if cpu: out(' ' + fmt_cpu(cpu))
            tm = tn = 0
            for name, r in rows.items():
                if 'error' in r: out(f'  {name}: 오류 {r["error"]}'); continue
                tm += r['missing']; tn += r['saved'] + r['missing']
                out(f"  {name:7s} 저장 {r['saved']:5d} (기대 {a.seconds * F}) · 누락 {r['missing']:4d} ({r['pct']:4.1f}%) · 끊김 {r['events']:3d} · 최대간격 {r['max_gap']:5.0f}ms · 처리큐 드롭 {r['dropped']}")
            summary['C'] = 100.0 * tm / max(1, tn)
            out(f"  → 전체 누락 {tm}장 ({summary['C']:.1f}%)")
        except Exception as e:
            out(f'  실패: {type(e).__name__}: {e}')

    out('\n' + '-' * 78)
    out('요약 (컬러 누락률): ' + ' · '.join(f'{k} {v:.1f}%' for k, v in summary.items()))
    OK = 1.0   # 1% 미만이면 정상으로 본다
    A, D, B, C = (summary.get(k) for k in 'ADBC')
    if A is not None and A >= OK:
        if D is not None and D < OK: out('판정: 1대씩은 정상, 4대 동시에 빠짐 → USB 대역폭 / 컨트롤러 공유 문제. 카메라를 서로 다른 USB 컨트롤러(또는 PCIe USB 카드)로 나눠 꽂으세요.')
        else: out('판정: 받기만 해도 빠짐 → USB 케이블 · 포트 · 카메라 · 드라이버 쪽. 많이 빠지는 카메라의 케이블 · 포트를 바꿔 보세요.')
    elif B is not None and B >= OK: out('판정: 받기만 하면 정상, 정렬(align)을 붙이면 빠짐 → 정렬 · 프레임 버퍼 문제 (코드로 해결: 녹화 중 정렬하지 않고 원본 저장 후 정렬).')
    elif C is not None and C >= OK: out('판정: 정렬까지는 정상, 실제 저장을 붙이면 빠짐 → 저장(mp4 · npy) · 디스크 · CPU 부하 문제 (코드로 해결 가능).')
    elif summary and summary.get('L', 0) < OK and summary.get('Q', 0) < OK: out('판정: 모든 단계에서 누락 1% 미만 — 지금 조건에서는 재현되지 않음. 실제 촬영 때(조명 · 작업자 동작 · 오래 켜 둔 상태) 다시 실행해 보세요.')
    L, Q = summary.get('L'), summary.get('Q')
    if L is not None and Q is not None:
        if L >= OK and Q < OK: out('부하 판정: 촬영 서버가 바쁘면 예전 방식은 빠지고(L), SDK 버퍼로 받으면 안 빠짐(Q) → SDK 버퍼(capture.json sdk_queue, 기본 32)로 해결되는 누락.')
        elif L >= OK and Q >= OK: out('부하 판정: SDK 버퍼로 받아도 빠짐(Q) → 버퍼로는 부족. 촬영 서버의 부하 자체를 줄여야 함 (화면 응답 · QC 를 다른 프로세스로).')
        elif Q >= OK: out('부하 판정: 예전 방식은 정상인데 SDK 버퍼에서 빠짐(Q) → capture.json 에 "sdk_queue": 0 을 넣어 예전 방식으로 돌리세요.')
        else: out('부하 판정: 파이썬 부하를 걸어도 두 방식 모두 누락 1% 미만.')
    log_dir = ROOT / 'logs'; log_dir.mkdir(exist_ok=True)
    f = log_dir / f'diag-frames-{datetime.now():%Y%m%d-%H%M%S}.txt'
    f.write_text('\n'.join(LINES) + '\n', encoding='utf-8')
    out(f'\n결과 파일: {f}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
