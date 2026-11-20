"""RealSense RGB-D 캡처 백엔드 — 다중 D455 동시 녹화.

GoPro(``service.ExoService``)가 SD카드 녹화→다운로드 모델인 것과 달리, RealSense는
SD카드가 없어 워커 PC가 스트림을 직접 받아 로컬 파일로 기록한다. 한 대당:

    {out_dir}/{cam_name}.mp4                  ← color (8-bit BGR, H.264)
    {out_dir}/{cam_name}_depth/frame_000001.png ← depth (16-bit, PNG16, color 정렬)
    {out_dir}/{cam_name}_intrinsics.json        ← color/depth intrinsics + depth_scale

Depth 는 16-bit 라 color mp4(yuv420p 8-bit)에 담을 수 없어 PNG16 시퀀스로 분리 저장한다.
depth 는 ``rs.align(rs.stream.color)`` 로 color 프레임에 정렬되어, 같은 인덱스의
color/depth 프레임이 픽셀 대응된다.

이 모듈은 ``pyrealsense2`` 가 없는 환경(예: 개발용 macOS)에서도 import 가 통과하도록
라이브러리 로딩을 지연/가드한다. 실제 캡처는 D455 가 연결된 워커 PC 에서만 동작한다.

Sources (verified 2026-06-15):
- librealsense Python API: https://intelrealsense.github.io/librealsense/python_docs/
- D455 권장 스트림: color 1280x720@30, depth 1280x720@30
- depth_scale(미터/단위): ``rs.depth_sensor.get_depth_scale()`` — D455 기본 0.001
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 캡처 프로파일 — 4대 공통 640×480@30fps (컬러 · 깊이)
# 1280×720@30fps × 4대 = 440 MB/s → USB 허브 400 MB/s 초과 → 허브 트립
# 848×480@30 은 USB 2.x 로 붙은 D455 가 컬러를 낼 수 없다('Couldn't resolve requests') — 640×480@30 은 USB 2.x 에서도 된다
# 4대 같은 해상도 · FPS → 하드웨어 동기화 · Phase 1 시각 맞추기 유지 (2026-11-10 결정)
COLOR_W, COLOR_H, COLOR_FPS = 640, 480, 30
DEPTH_W, DEPTH_H, DEPTH_FPS = 640, 480, 30


def _rs():
    """pyrealsense2 모듈을 지연 import. 미설치 시 RuntimeError."""
    try:
        import pyrealsense2 as rs  # type: ignore
        return rs
    except ImportError as e:
        raise RuntimeError(
            "pyrealsense2(librealsense) 가 설치되지 않았습니다. "
            "RealSense RGB-D 캡처는 D455 드라이버가 설치된 워커 PC 에서만 동작합니다."
        ) from e


def realsense_available() -> bool:
    try:
        import pyrealsense2  # noqa: F401
        return True
    except ImportError:
        return False


def discover_devices() -> list[str]:
    """연결된 RealSense 기기 시리얼 목록(정렬됨). 미연결/미설치 시 빈 리스트."""
    try:
        rs = _rs()
    except RuntimeError:
        return []
    ctx = rs.context()
    serials = []
    for dev in ctx.query_devices():
        try:
            serials.append(dev.get_info(rs.camera_info.serial_number))
        except Exception:
            continue
    # 시리얼 정렬 → 세션 간 cam_N 라벨 안정 (GoPro base_url 정렬과 동일 취지)
    return sorted(serials)


class RealSenseRecorder:
    """단일 D455 color+depth 동기 녹화기 — 캡처/쓰기 분리 구조.

    캡처 스레드: USB 스트림을 받아 프레임 큐에 넣음 (디스크 I/O 없음 → 드롭 최소화)
    쓰기 스레드: 큐에서 꺼내 color MP4 + depth .npy 저장 (느려도 캡처에 영향 없음)

    depth는 로컬에 .npy(uint16)로 저장하고, 업로드 시 PNG16으로 변환한다.
    """

    # 큐 최대 크기: 60프레임(2초) — .npy 쓰기는 ~3ms라 거의 쌓이지 않음
    _QUEUE_MAXSIZE = 60

    def __init__(self, serial: str) -> None:
        self.serial = serial
        self._capture_thread: Optional[threading.Thread] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._frame_queue: queue.Queue = queue.Queue(maxsize=self._QUEUE_MAXSIZE)
        self._running = False
        self._out: dict = {}
        self._capture_error: Optional[str] = None
        self._writer_error: Optional[str] = None
        self._live_frame_count: int = 0
        self._ready_event: Optional[threading.Event] = None
        self._go_event: Optional[threading.Event] = None

    @property
    def live_frame_count(self) -> int:
        return self._live_frame_count

    # ── Public ──────────────────────────────────────────────────────────────

    def start(self, out_dir: str | Path, cam_name: str,
              ready_event: Optional[threading.Event] = None,
              go_event: Optional[threading.Event] = None) -> None:
        if self._running:
            raise RuntimeError(f"{self.serial}: 이미 녹화 중")
        self._ready_event = ready_event
        self._go_event = go_event
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        depth_dir = out_dir / f"{cam_name}_depth"
        depth_dir.mkdir(parents=True, exist_ok=True)

        self._out = {
            "color_path":      str(out_dir / f"{cam_name}.mp4"),
            "depth_dir":       str(depth_dir),
            "depth_format":    "npy",
            "intrinsics_path": str(out_dir / f"{cam_name}_intrinsics.json"),
            "timestamps_path": str(out_dir / f"{cam_name}_timestamps.csv"),
            "frame_count":     0,
        }
        self._capture_error = None
        self._writer_error = None
        self._live_frame_count = 0
        self._running = True

        self._capture_thread = threading.Thread(
            target=self._capture_loop, args=(cam_name,), daemon=True,
            name=f"rs-capture-{self.serial[-6:]}"
        )
        self._writer_thread = threading.Thread(
            target=self._write_loop, args=(cam_name,), daemon=True,
            name=f"rs-writer-{self.serial[-6:]}"
        )
        self._capture_thread.start()
        self._writer_thread.start()

    def stop(self) -> dict:
        """녹화 종료. 캡처 스레드 먼저 정지 → 쓰기 스레드가 큐를 모두 소진 후 종료."""
        self._running = False
        if self._capture_thread:
            self._capture_thread.join(timeout=10.0)
        # 캡처 스레드가 None 센티넬을 큐에 넣으면 쓰기 스레드가 자동 종료
        if self._writer_thread:
            self._writer_thread.join(timeout=120.0)
        error = self._capture_error or self._writer_error
        if error:
            raise RuntimeError(f"{self.serial}: {error}")
        return dict(self._out)

    # ── 캡처 스레드 ─────────────────────────────────────────────────────────

    def _capture_loop(self, cam_name: str) -> None:
        try:
            self._do_capture(cam_name)
        except Exception as e:
            self._capture_error = str(e)
            logger.exception(f"RealSense {self.serial} 캡처 스레드 실패")
        finally:
            # 초기화 실패 시 ready_event를 set해야 메인 스레드가 15초 대기 없이 즉시 에러 감지
            if self._ready_event and not self._ready_event.is_set():
                self._ready_event.set()
            self._frame_queue.put(None)   # 쓰기 스레드 종료 신호

    def _do_capture(self, cam_name: str) -> None:
        import numpy as np
        rs = _rs()

        pipeline = rs.pipeline()
        config = rs.config()
        config.enable_device(self.serial)
        config.enable_stream(rs.stream.color, COLOR_W, COLOR_H, rs.format.bgr8, COLOR_FPS)
        config.enable_stream(rs.stream.depth, DEPTH_W, DEPTH_H, rs.format.z16, DEPTH_FPS)

        profile = pipeline.start(config)
        align = rs.align(rs.stream.color)

        # intrinsics + depth_scale 추출
        depth_sensor = profile.get_device().first_depth_sensor()
        depth_scale = float(depth_sensor.get_depth_scale())
        color_prof = profile.get_stream(rs.stream.color).as_video_stream_profile()
        ci = color_prof.get_intrinsics()
        intr = {
            "serial":      self.serial,
            "camera_name": cam_name,
            "depth_scale": depth_scale,
            "color_intrinsics": {
                "width": ci.width, "height": ci.height,
                "fx": ci.fx, "fy": ci.fy, "ppx": ci.ppx, "ppy": ci.ppy,
                "model": str(ci.model), "coeffs": list(ci.coeffs),
            },
            "aligned_to": "color",
            "color_fps": COLOR_FPS, "depth_fps": DEPTH_FPS,
        }
        Path(self._out["intrinsics_path"]).write_text(
            json.dumps(intr, indent=2, ensure_ascii=False)
        )

        # 모든 카메라가 준비될 때까지 대기 → 동시 캡처 시작
        if self._ready_event:
            self._ready_event.set()
        if self._go_event:
            self._go_event.wait()

        dropped = 0
        frame_idx = 0
        # 첫 프레임 미수신 시 조기 진단 — USB 허브 트립/카메라 불안정 감지용
        first_frame_deadline = time.time() + 10.0

        try:
            while self._running:
                try:
                    frames = pipeline.wait_for_frames(timeout_ms=500)
                except RuntimeError:
                    # 첫 프레임이 10초 넘게 안 오면 에러 → STOP 전에 UI가 알 수 있음
                    if frame_idx == 0 and time.time() > first_frame_deadline:
                        raise RuntimeError(
                            f"{self.serial}: 10초 내 첫 프레임 미수신 — "
                            "USB 허브 전원/연결 확인 후 USB 리셋 버튼을 누르세요"
                        )
                    continue
                frames = align.process(frames)
                color = frames.get_color_frame()
                depth = frames.get_depth_frame()
                if not color or not depth:
                    continue

                if frame_idx == 0:
                    logger.info(f"[{self.serial}] ({cam_name}) 첫 프레임 수신 — 녹화 시작")

                hw_ts  = color.get_timestamp()   # 카메라 내부 클락 ms
                host_t = time.time()             # 호스트 도착 시각 s

                # RealSense 버퍼는 다음 wait_for_frames()에서 재사용됨 → 반드시 copy
                color_img = np.asanyarray(color.get_data()).copy()
                depth_img = np.asanyarray(depth.get_data()).copy()

                try:
                    self._frame_queue.put_nowait(
                        (frame_idx, color_img, depth_img, hw_ts, host_t)
                    )
                    frame_idx += 1
                except queue.Full:
                    dropped += 1
                    logger.warning(
                        f"{self.serial}: 쓰기 큐 가득 참 — 프레임 {frame_idx} 드롭 (총 {dropped})"
                    )
        finally:
            pipeline.stop()
            if dropped:
                logger.warning(f"{self.serial}({cam_name}): 총 {dropped}프레임 드롭")

    # ── 쓰기 스레드 ─────────────────────────────────────────────────────────

    def _write_loop(self, cam_name: str) -> None:
        try:
            self._do_write(cam_name)
        except Exception as e:
            self._writer_error = str(e)
            logger.exception(f"RealSense {self.serial} 쓰기 스레드 실패")

    def _do_write(self, cam_name: str) -> None:
        import cv2
        import numpy as np

        color_path = self._out["color_path"]
        depth_dir  = Path(self._out["depth_dir"])
        ts_path    = Path(self._out["timestamps_path"])

        writer = cv2.VideoWriter(
            color_path,
            cv2.VideoWriter_fourcc(*"mp4v"),
            COLOR_FPS,
            (COLOR_W, COLOR_H),
        )
        frame_count = 0
        try:
            with open(ts_path, "w") as ts_f:
                ts_f.write("frame_idx,hw_timestamp_ms,host_time_s\n")
                while True:
                    item = self._frame_queue.get()
                    if item is None:
                        break
                    frame_idx, color_img, depth_img, hw_ts, host_t = item

                    writer.write(color_img)
                    # depth: .npy (uint16) — PNG 압축 없이 raw 저장 → write ~3ms
                    np.save(str(depth_dir / f"frame_{frame_idx:06d}.npy"), depth_img)
                    ts_f.write(f"{frame_idx},{hw_ts:.3f},{host_t:.6f}\n")
                    frame_count += 1
                    self._live_frame_count = frame_count
        finally:
            writer.release()
            self._out["frame_count"] = frame_count
            logger.info(
                f"RealSense {self.serial}({cam_name}): {frame_count}프레임 저장 완료"
            )


class RealSenseSession:
    """다중 D455 동시 녹화 세션 관리 — ExoService(realsense backend)에서 사용."""

    def __init__(self) -> None:
        self._recorders: list[RealSenseRecorder] = []
        self._camera_names: list[str] = []

    def start(self, out_dir: str | Path) -> list[str]:
        """연결된 모든 D455 동시 녹화 시작. cam_N 라벨 리스트 반환.

        각 카메라가 USB 파이프라인 초기화(pipeline.start)를 완료한 뒤
        go_event로 일제히 캡처를 시작하여 프레임 0 타임스탬프를 최대한 동기화.
        """
        serials = discover_devices()
        if not serials:
            raise RuntimeError("연결된 RealSense 기기가 없습니다")

        self._recorders = [RealSenseRecorder(s) for s in serials]
        self._camera_names = [f"cam_{i + 1}" for i in range(len(serials))]

        ready_events = [threading.Event() for _ in serials]
        go_event = threading.Event()

        for rec, name, ev in zip(self._recorders, self._camera_names, ready_events):
            rec.start(out_dir, name, ready_event=ev, go_event=go_event)

        # 모든 카메라가 USB 초기화 완료될 때까지 대기
        try:
            for i, (ev, rec) in enumerate(zip(ready_events, self._recorders)):
                if not ev.wait(timeout=15.0):
                    raise RuntimeError(f"cam_{i+1} 이 15초 내 초기화되지 않음 — USB 연결 확인")
                if rec._capture_error:
                    raise RuntimeError(f"cam_{i+1} 초기화 실패: {rec._capture_error}")
        except Exception:
            # 한 대라도 실패하면 이미 연 카메라를 모두 닫는다 — 그대로 두면 go 신호를 기다리며 장치를 계속 잡아
            # 다음 녹화 시작이 'Device or resource busy' 로 실패한다 (서버를 다시 띄워야 풀림)
            self._abort(go_event)
            raise

        # 전 카메라 동시 캡처 시작
        go_event.set()
        return list(self._camera_names)

    def _abort(self, go_event: threading.Event) -> None:
        """녹화 시작 실패 — 모든 캡처 스레드를 멈추고 파이프라인을 닫는다 (기록할 프레임 없음)."""
        for rec in self._recorders:
            rec._running = False
        go_event.set()   # go 를 기다리던 스레드가 깨어나 _running=False 를 보고 바로 끝난다 → finally 에서 pipeline.stop()
        for rec in self._recorders:
            for t, timeout in ((getattr(rec, "_capture_thread", None), 10.0), (getattr(rec, "_writer_thread", None), 30.0)):
                if t is not None:
                    t.join(timeout=timeout)
                    if t.is_alive():
                        logger.warning(f"RealSense {rec.serial}: 녹화 시작 실패 정리 — {t.name} 가 {timeout}초 안에 끝나지 않음")
        logger.info("RealSense 녹화 시작 실패 — 연 카메라를 모두 닫음")
        self._recorders = []
        self._camera_names = []

    def get_live_status(self) -> dict[str, int]:
        """녹화 중 카메라별 실시간 프레임 수. UI 폴링용."""
        return {
            name: rec.live_frame_count
            for name, rec in zip(self._camera_names, self._recorders)
        }

    def stop(self) -> dict:
        """전 기기 녹화 동시 종료. 모든 캡처 스레드에 정지 신호를 보낸 뒤 병렬 join.

        순차 stop()은 cam_1이 join 완료될 때까지 cam_2/3/4가 계속 녹화해
        카메라 간 프레임 수 차이가 수백 프레임 발생하는 문제가 있었음.
        """
        results: dict = {}
        lock = threading.Lock()

        def _stop_one(rec: RealSenseRecorder, name: str) -> None:
            try:
                r = rec.stop()
            except Exception as e:
                r = {"error": str(e)}
            with lock:
                results[name] = r

        threads = [
            threading.Thread(target=_stop_one, args=(rec, name), daemon=True)
            for rec, name in zip(self._recorders, self._camera_names)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=130.0)

        self._recorders = []
        self._camera_names = []
        return results
