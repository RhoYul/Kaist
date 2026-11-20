"""
Exo-video 워커 PC 카메라 서비스.
USB / RTSP 카메라를 OpenCV로 캡처하고 MJPEG 스트림과 상태 정보를 제공.
RealSense D455는 pyrealsense2로 color 스트림을 캡처한다.

환경변수:
  EXO_CAMERAS   쉼표 구분 카메라 목록 (기본: realsense)
                형식: <camera_id>:<source>
                source = USB 인덱스(정수), RTSP URL, 또는 "realsense"
  예) EXO_CAMERAS=realsense              ← D455 자동 발견
      EXO_CAMERAS=cam1:0,cam2:1          ← OpenCV USB
      EXO_CAMERAS=cam1:realsense:<SN>    ← 특정 시리얼 D455
"""
import logging
import os
import time
import threading
from typing import Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

try:
    import pyrealsense2 as rs
    import numpy as np
    _RS_AVAILABLE = True
except ImportError:
    _RS_AVAILABLE = False


class CameraCapture:
    def __init__(self, camera_id: str, source: str) -> None:
        self.camera_id = camera_id
        self.source = source
        self.connected = False
        self.fps = 0.0
        self.resolution = ""
        self._frame: Optional[bytes] = None
        self._lock = threading.Lock()
        self._frame_times: List[float] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not _CV2_AVAILABLE:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        src = int(self.source) if self.source.isdigit() else self.source
        cap = cv2.VideoCapture(src)
        while self._running:
            if not cap.isOpened():
                self.connected = False
                cap.release()
                time.sleep(2.0)
                cap = cv2.VideoCapture(src)
                continue
            ret, frame = cap.read()
            if not ret:
                self.connected = False
                time.sleep(0.05)
                continue
            _, buf = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80]
            )
            jpeg = buf.tobytes()
            t = time.time()
            with self._lock:
                self._frame = jpeg
                self.connected = True
                self._frame_times.append(t)
                self._frame_times = [ft for ft in self._frame_times if t - ft < 2.0]
                self.fps = round(len(self._frame_times) / 2.0, 1)
                h, w = frame.shape[:2]
                self.resolution = f"{w}x{h}"
        cap.release()

    def get_frame_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._frame

    def get_status(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "source": self.source,
            "connected": self.connected,
            "fps": self.fps,
            "resolution": self.resolution,
        }

    def stop(self) -> None:
        self._running = False


class RealSenseCameraCapture:
    """D455 단일 카메라 color 스트림 캡처 (프리뷰/상태용)."""

    def __init__(self, camera_id: str, serial: str) -> None:
        self.camera_id = camera_id
        self.serial = serial
        self.source = f"realsense:{serial}"
        self.connected = False
        self.fps = 0.0
        self.resolution = ""
        self._frame: Optional[bytes] = None
        self._lock = threading.Lock()
        self._frame_times: List[float] = []
        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        # pipeline이 실제로 stop()된 뒤 set → Depth ICP 진입 전 대기에 사용
        self._pipeline_released = threading.Event()
        self._pipeline_released.set()  # 초기 상태: 파이프라인 없음 = released
        # 파이프라인 최초 기동 시 팩토리 인트린직 캐싱
        self._factory_intrinsics: Optional[dict] = None

    def start(self) -> None:
        if not _RS_AVAILABLE:
            return
        self._running = True
        self._paused = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        """녹화 시작 전 프리뷰 파이프라인 해제 — 카메라 리소스 반납."""
        self._paused = True
        with self._lock:
            self.connected = False
            self._frame = None

    def resume(self) -> None:
        """녹화 종료 후 프리뷰 파이프라인 재시작."""
        self._paused = False

    def _loop(self) -> None:
        while self._running:
            if self._paused:
                self._pipeline_released.set()  # 파이프라인 없음 = released
                time.sleep(0.5)
                continue
            pipeline = rs.pipeline()
            config = rs.config()
            config.enable_device(self.serial)
            # 프리뷰용 저해상도 — 1280x720@30fps 4대 동시 기동 시 USB 과부하로 허브 차단됨
            config.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 15)
            try:
                self._pipeline_released.clear()  # 파이프라인 시작 — not released
                profile = pipeline.start(config)
                logger.info(f"[preview] {self.camera_id} ({self.serial}) 카메라 열림")
                if self._factory_intrinsics is None:
                    try:
                        color_profile = profile.get_stream(rs.stream.color).as_video_stream_profile()
                        intr = color_profile.get_intrinsics()
                        import numpy as _np
                        K = _np.array([[intr.fx, 0, intr.ppx],
                                       [0, intr.fy, intr.ppy],
                                       [0,       0,        1]], dtype=_np.float64)
                        self._factory_intrinsics = {
                            "K": K,
                            "dist": _np.array(intr.coeffs, dtype=_np.float64),
                            "size": (intr.width, intr.height),
                        }
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"[preview] {self.camera_id} ({self.serial}) 카메라 열기 실패: {e}")
                self._pipeline_released.set()    # 시작 실패 → 리소스 없음
                self.connected = False
                time.sleep(3.0)
                continue
            try:
                while self._running and not self._paused:
                    frames = pipeline.wait_for_frames(timeout_ms=3000)
                    color = frames.get_color_frame()
                    if not color:
                        continue
                    img = np.asanyarray(color.get_data())
                    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    jpeg = buf.tobytes()
                    t = time.time()
                    with self._lock:
                        self._frame = jpeg
                        self.connected = True
                        self._frame_times.append(t)
                        self._frame_times = [ft for ft in self._frame_times if t - ft < 2.0]
                        self.fps = round(len(self._frame_times) / 2.0, 1)
                        h, w = img.shape[:2]
                        self.resolution = f"{w}x{h}"
            except Exception as e:
                logger.warning(f"[preview] {self.camera_id} ({self.serial}) 프레임 받기 실패: {e}")
                self.connected = False
            finally:
                try:
                    pipeline.stop()
                except Exception as e:
                    logger.warning(f"[preview] {self.camera_id} ({self.serial}) 카메라 닫기 오류: {e}")
                self._pipeline_released.set()    # stop() 완료 → 리소스 반납됨
                logger.info(f"[preview] {self.camera_id} ({self.serial}) 카메라 놓음 (paused={self._paused})")

    def get_frame_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._frame

    def get_factory_intrinsics(self) -> Optional[dict]:
        return self._factory_intrinsics

    def get_status(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "source": self.source,
            "connected": self.connected,
            "fps": self.fps,
            "resolution": self.resolution,
            "serial": self.serial,
            "backend": "realsense",
        }

    def stop(self) -> None:
        self._running = False


def _discover_realsense_serials() -> List[str]:
    if not _RS_AVAILABLE:
        return []
    try:
        ctx = rs.context()
        serials = []
        for dev in ctx.query_devices():
            try:
                serials.append(dev.get_info(rs.camera_info.serial_number))
            except Exception:
                continue
        return sorted(serials)
    except Exception:
        return []


class ExoCameraService:
    _instance: Optional["ExoCameraService"] = None
    # 서버를 띄운 직후 미리보기 요청(cam_1~4)이 동시에 오면 객체가 여러 개 만들어져 카메라마다 미리보기 스레드가 겹친다
    # → 같은 카메라를 두 스레드가 열어 busy, 녹화 전 pause() 도 한 객체에만 간다. 잠금으로 하나만 만든다
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._cameras: Dict[str, object] = {}
        self._paused = False
        self._load()

    @classmethod
    def get(cls) -> "ExoCameraService":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
                    logger.info(f"[preview] 카메라 관리 객체 생성 — 카메라 {len(cls._instance._cameras)}대")
        return cls._instance

    def pause(self) -> None:
        """녹화 시작 전 모든 프리뷰 파이프라인 해제."""
        self._paused = True
        for cam in self._cameras.values():
            if hasattr(cam, "pause"):
                cam.pause()

    def resume(self) -> None:
        """녹화 종료 후 모든 프리뷰 파이프라인 재시작."""
        self._paused = False
        for cam in self._cameras.values():
            if hasattr(cam, "resume"):
                cam.resume()

    def reload(self) -> int:
        """기존 카메라 스레드 종료 후 USB 재탐색해 재기동. 발견된 카메라 수 반환.

        kill -9 등으로 파이프라인이 비정상 종료된 뒤 USB 리셋 완료 시 호출.
        """
        # 기존 스레드 정지
        for cam in self._cameras.values():
            cam.stop()
        self._cameras.clear()
        self._paused = False
        # 재탐색 + 재기동
        self._load()
        return len(self._cameras)

    def is_paused(self) -> bool:
        return self._paused

    def wait_for_all_released(self, timeout: float = 12.0) -> bool:
        """모든 RealSense 파이프라인이 stop()될 때까지 블로킹 대기.

        Returns True if all released within timeout, False otherwise.
        """
        deadline = time.time() + timeout
        ok = True
        for cam in self._cameras.values():
            ev = getattr(cam, "_pipeline_released", None)
            if ev is None:
                continue
            remaining = deadline - time.time()
            if remaining <= 0 or not ev.wait(timeout=remaining):
                ok = False
                break
        held = [f"{getattr(c, 'camera_id', '?')}({getattr(c, 'serial', '?')})" for c in self._cameras.values()
                if getattr(c, "_pipeline_released", None) is not None and not c._pipeline_released.is_set()]
        logger.info(f"[preview] 녹화 전 카메라 해제 대기 {timeout}초 — {'모두 풀림' if ok else '안 풀린 카메라: ' + ', '.join(held)}")
        return ok

    def _load(self) -> None:
        env = os.getenv("EXO_CAMERAS", "realsense")

        # EXO_CAMERAS=realsense → D455 자동 발견
        if env.strip().lower() == "realsense":
            serials = _discover_realsense_serials()
            for i, serial in enumerate(serials):
                cam_id = f"cam_{i + 1}"
                cam = RealSenseCameraCapture(cam_id, serial)
                cam.start()
                self._cameras[cam_id] = cam
            return

        for entry in env.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" in entry:
                parts = entry.split(":", 2)
                cam_id = parts[0].strip()
                source = parts[1].strip()
                # realsense:<serial> 형식
                if source.lower() == "realsense" and len(parts) == 3:
                    serial = parts[2].strip()
                    cam = RealSenseCameraCapture(cam_id, serial)
                    cam.start()
                    self._cameras[cam_id] = cam
                    continue
            else:
                cam_id = source = entry

            cam = CameraCapture(cam_id.strip(), source.strip() if ":" not in entry else entry.split(":", 1)[1].strip())
            cam.start()
            self._cameras[cam_id.strip()] = cam

    def list_cameras(self) -> List[dict]:
        return [c.get_status() for c in self._cameras.values()]

    def get_status(self, camera_id: str) -> Optional[dict]:
        cam = self._cameras.get(camera_id)
        return cam.get_status() if cam else None

    def get_realsense_serials(self) -> List[tuple]:
        """카메라 등록 순서대로 [(cam_id, serial), ...] 반환 — RealSense 전용."""
        return [
            (cam_id, cam.serial)
            for cam_id, cam in self._cameras.items()
            if isinstance(cam, RealSenseCameraCapture)
        ]

    def get_factory_intrinsics_list(self, camera_names: Optional[List[str]] = None) -> Optional[List[Optional[dict]]]:
        """캘리브레이션용 팩토리 인트린직 목록 반환.

        camera_names 지정 시 해당 순서대로, 없으면 등록 순서대로.
        파이프라인이 아직 한 번도 기동되지 않은 카메라는 None.
        RealSense가 아닌 카메라도 None.
        """
        keys = camera_names if camera_names else list(self._cameras.keys())
        result = []
        for cam_id in keys:
            cam = self._cameras.get(cam_id)
            if cam is not None and isinstance(cam, RealSenseCameraCapture):
                result.append(cam.get_factory_intrinsics())
            else:
                result.append(None)
        return result

    def capture_all_jpeg(self) -> List[dict]:
        """모든 카메라에서 현재 프레임 JPEG을 동시에 가져옵니다.

        Returns
        -------
        list of {"cam_id": str, "jpeg": bytes}
        카메라 등록 순서(cam_1, cam_2, ...)대로 정렬됩니다.
        """
        result = []
        for cam_id, cam in self._cameras.items():
            jpeg = cam.get_frame_jpeg()
            if jpeg:
                result.append({"cam_id": cam_id, "jpeg": jpeg})
        return result

    def mjpeg_frames(self, camera_id: str) -> Generator[bytes, None, None]:
        cam = self._cameras.get(camera_id)
        if not cam:
            return
        while True:
            jpeg = cam.get_frame_jpeg()
            if jpeg:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + jpeg
                    + b"\r\n"
                )
            time.sleep(1 / 30)
