"""Exo extrinsic calibration — inter-camera R/T computation.

두 가지 캘리브레이션 방식을 지원합니다:
  compute_extrinsics()         — 일반 체커보드 (findChessboardCorners)
  compute_extrinsics_charuco() — ChArUco 보드 (partial visibility 지원, D455 권장)

Result format:
    {
        "ok": true,
        "cameras": ["cam_1", "cam_2", ...],
        "transforms": {
            "cam_1": {"R": [[...]], "T": [...]},   # relative to cam_0
            ...
        },
        "reprojectionErrors": {"cam_1": 0.42, ...},
    }
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

MIN_SYNC_IMAGES = 5


class ExtrinsicCalibrationError(Exception):
    def __init__(self, message: str, *, detected: int = 0, total: int = 0):
        super().__init__(message)
        self.detected = detected
        self.total = total


def compute_extrinsics(
    images_per_camera: list[list[bytes]],
    *,
    board_size: tuple[int, int] = (9, 6),
    square_size_mm: float = 25.0,
    camera_names: Optional[list[str]] = None,
    factory_intrinsics: Optional[list] = None,
) -> dict:
    """Compute extrinsic calibration between N cameras.

    Parameters
    ----------
    images_per_camera : list[list[bytes]]
        Outer list: one entry per camera.
        Inner list: synchronized captures (same board position, same moment).
        len(images_per_camera[i]) must equal len(images_per_camera[j]) for all i,j.
    board_size : (cols, rows)
        Inner-corner count of the checkerboard.
    square_size_mm : float
        Physical size of each checker square.
    camera_names : list[str] | None
        Names for each camera (defaults to cam_1, cam_2, ...).

    Returns
    -------
    dict
        ok, cameras, transforms, reprojectionErrors
    """
    try:
        import cv2
        import numpy as np
    except ImportError as e:
        raise ExtrinsicCalibrationError(
            "OpenCV가 설치되어 있지 않습니다. `pip install opencv-contrib-python` 을 실행하세요."
        ) from e

    n_cams = len(images_per_camera)
    if n_cams < 2:
        raise ExtrinsicCalibrationError(
            f"extrinsic 캘리브레이션은 최소 2대 카메라가 필요합니다 (받은 수: {n_cams})"
        )

    names = camera_names or [f"cam_{i + 1}" for i in range(n_cams)]
    n_sync = len(images_per_camera[0])
    if n_sync < MIN_SYNC_IMAGES:
        raise ExtrinsicCalibrationError(
            f"동기화 이미지가 부족합니다 (최소 {MIN_SYNC_IMAGES}장, 받은 수: {n_sync})"
        )

    # 3D object points for one board pose
    objp = np.zeros((board_size[0] * board_size[1], 3), dtype=np.float32)
    objp[:, :2] = np.mgrid[0:board_size[0], 0:board_size[1]].T.reshape(-1, 2)
    objp *= square_size_mm

    # Detect corners in each camera's images
    all_obj_pts: list[list] = [[] for _ in range(n_cams)]
    all_img_pts: list[list] = [[] for _ in range(n_cams)]
    img_sizes: list[Optional[tuple]] = [None] * n_cams
    valid_frames: list[set] = [set() for _ in range(n_cams)]

    for cam_idx, cam_images in enumerate(images_per_camera):
        for frame_idx, img_bytes in enumerate(cam_images):
            arr = np.frombuffer(img_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            if img_sizes[cam_idx] is None:
                img_sizes[cam_idx] = (img.shape[1], img.shape[0])
            ret, corners = cv2.findChessboardCorners(gray, board_size, None)
            if ret:
                criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
                corners_sub = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
                all_obj_pts[cam_idx].append(objp)
                all_img_pts[cam_idx].append(corners_sub)
                valid_frames[cam_idx].add(frame_idx)

    # Keep only frames detected in ALL cameras (synchronized poses)
    common_frames = valid_frames[0]
    for vf in valid_frames[1:]:
        common_frames = common_frames & vf

    if len(common_frames) < MIN_SYNC_IMAGES:
        total = n_sync
        detected = len(common_frames)
        raise ExtrinsicCalibrationError(
            f"공통으로 체커보드가 검출된 프레임이 부족합니다 "
            f"(최소 {MIN_SYNC_IMAGES}장, 공통 검출: {detected}/{total})",
            detected=detected,
            total=total,
        )

    # Intrinsic: 팩토리 값 우선 사용, 없을 때만 calibrateCamera 추정
    intrinsics: list[dict] = []
    for cam_idx in range(n_cams):
        obj_pts_common = [all_obj_pts[cam_idx][i] for i in sorted(common_frames) if i < len(all_obj_pts[cam_idx])]
        img_pts_common = [all_img_pts[cam_idx][i] for i in sorted(common_frames) if i < len(all_img_pts[cam_idx])]
        sz = img_sizes[cam_idx]
        if not obj_pts_common or sz is None:
            raise ExtrinsicCalibrationError(f"{names[cam_idx]}: 코너 검출 실패")
        fi = (factory_intrinsics[cam_idx] if factory_intrinsics and cam_idx < len(factory_intrinsics) else None)
        if fi is not None:
            K, dist = fi["K"], fi["dist"]
        else:
            _, K, dist, _, _ = cv2.calibrateCamera(obj_pts_common, img_pts_common, sz, None, None)
        intrinsics.append({"K": K, "dist": dist, "size": sz, "obj_pts": obj_pts_common, "img_pts": img_pts_common})

    # Stereo calibration: each camera relative to cam_0 (reference)
    ref = intrinsics[0]
    transforms: dict[str, dict] = {}
    reproj_errors: dict[str, float] = {}

    for cam_idx in range(1, n_cams):
        cam = intrinsics[cam_idx]
        flags = cv2.CALIB_FIX_INTRINSIC
        ret, _, _, _, _, R, T, E, F = cv2.stereoCalibrate(
            ref["obj_pts"],
            ref["img_pts"],
            cam["img_pts"],
            ref["K"],
            ref["dist"],
            cam["K"],
            cam["dist"],
            ref["size"],
            flags=flags,
        )
        transforms[names[cam_idx]] = {
            "R": R.tolist(),
            "T": T.tolist(),
        }
        reproj_errors[names[cam_idx]] = round(float(ret), 4)

    return {
        "ok": True,
        "cameras": names,
        "transforms": transforms,
        "reprojectionErrors": reproj_errors,
    }


def compute_extrinsics_charuco(
    images_per_camera: list[list[bytes]],
    *,
    squares_x: int = 11,
    squares_y: int = 8,
    square_length_mm: float = 40.0,
    marker_length_mm: float = 30.0,
    aruco_dict_name: str = "DICT_4X4_50",
    camera_names: Optional[list[str]] = None,
    factory_intrinsics: Optional[list] = None,
) -> dict:
    """ChArUco 보드 기반 extrinsic calibration (OpenCV 4.8+ 신규 API 사용).

    Parameters
    ----------
    images_per_camera : list[list[bytes]]
        카메라별 동기화 캡처 이미지 (JPEG/PNG). 인덱스 순서: [cam][frame].
    squares_x, squares_y : int
        ChArUco 보드의 가로/세로 사각형 수.
    square_length_mm : float
        사각형 한 변의 실제 길이 (mm).
    marker_length_mm : float
        ArUco 마커 한 변의 실제 길이 (mm, square_length의 약 75%).
    aruco_dict_name : str
        cv2.aruco 딕셔너리 이름 (예: "DICT_4X4_50").
    camera_names : list[str] | None
        카메라 이름 목록 (없으면 cam_1, cam_2, ...).
    """
    try:
        import cv2
        import numpy as np
    except ImportError as e:
        raise ExtrinsicCalibrationError("OpenCV가 설치되어 있지 않습니다.") from e

    n_cams = len(images_per_camera)
    if n_cams < 2:
        raise ExtrinsicCalibrationError(
            f"extrinsic 캘리브레이션은 최소 2대 카메라가 필요합니다 (받은 수: {n_cams})"
        )

    names = camera_names or [f"cam_{i + 1}" for i in range(n_cams)]
    n_sync = len(images_per_camera[0])
    if n_sync < MIN_SYNC_IMAGES:
        raise ExtrinsicCalibrationError(
            f"동기화 이미지가 부족합니다 (최소 {MIN_SYNC_IMAGES}장, 받은 수: {n_sync})"
        )

    sq_len = square_length_mm / 1000.0
    mk_len = marker_length_mm / 1000.0

    try:
        aruco_dict = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, aruco_dict_name))
    except AttributeError:
        raise ExtrinsicCalibrationError(f"알 수 없는 ArUco 딕셔너리: {aruco_dict_name}")

    board = cv2.aruco.CharucoBoard((squares_x, squares_y), sq_len, mk_len, aruco_dict)
    detector = cv2.aruco.CharucoDetector(board)

    # board corner 3D 좌표 (ID 순서대로 정렬됨)
    board_obj_pts = board.getChessboardCorners()  # shape: (N_corners, 3)

    # 카메라별 검출 결과 수집 — frame_idx를 키로 사용 (append 순서 ≠ frame 번호 혼용 방지)
    all_corners: list[dict] = [{} for _ in range(n_cams)]
    all_ids: list[dict]     = [{} for _ in range(n_cams)]
    img_sizes: list[Optional[tuple]] = [None] * n_cams
    valid_frames: list[set] = [set() for _ in range(n_cams)]

    import os as _os, pathlib as _pl
    debug_dir = _pl.Path(__import__("tempfile").gettempdir()) / "capture_studio_calib_debug"
    debug_dir.mkdir(exist_ok=True)

    for cam_idx, cam_images in enumerate(images_per_camera):
        for frame_idx, img_bytes in enumerate(cam_images):
            arr = np.frombuffer(img_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                logger.warning(f"[charuco] cam{cam_idx} frame{frame_idx}: imdecode 실패")
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            if img_sizes[cam_idx] is None:
                img_sizes[cam_idx] = (img.shape[1], img.shape[0])

            # 이미지 저장 (진단용)
            cv2.imwrite(str(debug_dir / f"cam{cam_idx}_frame{frame_idx}.jpg"), img)

            charuco_corners, charuco_ids, _, _ = detector.detectBoard(gray)
            n_detected = len(charuco_ids) if charuco_ids is not None else 0
            logger.info(f"[charuco] cam{cam_idx} frame{frame_idx}: 검출된 코너 {n_detected}개")

            if charuco_corners is not None and charuco_ids is not None and len(charuco_ids) >= 6:
                all_corners[cam_idx][frame_idx] = charuco_corners
                all_ids[cam_idx][frame_idx]     = charuco_ids.flatten()
                valid_frames[cam_idx].add(frame_idx)
                # 검출 결과 시각화 저장
                vis = img.copy()
                cv2.aruco.drawDetectedCornersCharuco(vis, charuco_corners, charuco_ids)
                cv2.imwrite(str(debug_dir / f"cam{cam_idx}_frame{frame_idx}_detected.jpg"), vis)

    # 모든 카메라에서 공통으로 검출된 프레임만 사용
    common_frames = valid_frames[0]
    for vf in valid_frames[1:]:
        common_frames = common_frames & vf

    if len(common_frames) < MIN_SYNC_IMAGES:
        raise ExtrinsicCalibrationError(
            f"공통으로 ChArUco 마커가 검출된 프레임이 부족합니다 "
            f"(최소 {MIN_SYNC_IMAGES}장, 공통 검출: {len(common_frames)}/{n_sync})",
            detected=len(common_frames),
            total=n_sync,
        )

    # Intrinsic: 팩토리 값 우선 사용, 없을 때만 calibrateCamera 추정
    intrinsics: list[dict] = []
    sorted_common = sorted(common_frames)
    for cam_idx in range(n_cams):
        sz = img_sizes[cam_idx]
        fi = (factory_intrinsics[cam_idx] if factory_intrinsics and cam_idx < len(factory_intrinsics) else None)
        if fi is not None:
            K, dist = fi["K"], fi["dist"]
        else:
            c_list  = [all_corners[cam_idx][i] for i in sorted_common if i in all_corners[cam_idx]]
            id_list = [all_ids[cam_idx][i].reshape(-1, 1) for i in sorted_common if i in all_ids[cam_idx]]
            if not c_list or sz is None:
                raise ExtrinsicCalibrationError(f"{names[cam_idx]}: ChArUco 코너 검출 실패")
            obj_pts_list, img_pts_list = [], []
            for corners, ids in zip(c_list, id_list):
                op, ip = board.matchImagePoints(corners, ids)
                if op is not None and len(op) >= 6:
                    obj_pts_list.append(op)
                    img_pts_list.append(ip)
            if len(obj_pts_list) < 3:
                raise ExtrinsicCalibrationError(f"{names[cam_idx]}: intrinsic 캘리브레이션에 충분한 프레임이 없습니다")
            _, K, dist, _, _ = cv2.calibrateCamera(obj_pts_list, img_pts_list, sz, None, None)
        intrinsics.append({"K": K, "dist": dist, "size": sz})

    # 프레임별 공통 코너 ID로 stereo calibration용 포인트 준비
    obj_pts_all:   list = []
    ref_img_pts:   list = []
    cam_img_pts:   list[list] = [[] for _ in range(1, n_cams)]

    for fi in sorted_common:
        if fi not in all_ids[0]:
            continue
        common_ids = set(all_ids[0][fi])
        for ci in range(1, n_cams):
            if fi in all_ids[ci]:
                common_ids &= set(all_ids[ci][fi])
        if len(common_ids) < 6:
            continue

        cids = sorted(common_ids)
        ref_corners = all_corners[0][fi]
        ref_ids_arr = all_ids[0][fi]
        ref_pts = np.array(
            [ref_corners[int(np.where(ref_ids_arr == cid)[0][0])] for cid in cids],
            dtype=np.float32,
        )
        obj_pts = np.array([board_obj_pts[cid] for cid in cids], dtype=np.float32)
        obj_pts_all.append(obj_pts)
        ref_img_pts.append(ref_pts)

        for ci in range(1, n_cams):
            ci_corners = all_corners[ci][fi]   # fi는 dict 키 — 항상 존재 (common_frames 교집합)
            ci_ids_arr = all_ids[ci][fi]
            ci_pts = np.array(
                [ci_corners[int(np.where(ci_ids_arr == cid)[0][0])] for cid in cids],
                dtype=np.float32,
            )
            cam_img_pts[ci - 1].append(ci_pts)

    if len(obj_pts_all) < 3:
        raise ExtrinsicCalibrationError(
            f"공통 마커 코너가 있는 프레임이 부족합니다 (검출: {len(obj_pts_all)}, 최소 3). "
            "보드 중앙이 4대 카메라에 모두 보이도록 위치를 조정하세요."
        )

    # 카메라 0 기준 stereo calibration
    ref_intr = intrinsics[0]
    transforms: dict[str, dict] = {}
    reproj_errors: dict[str, float] = {}

    for cam_idx in range(1, n_cams):
        cam_intr = intrinsics[cam_idx]
        ret, _, _, _, _, R, T, _, _ = cv2.stereoCalibrate(
            obj_pts_all,
            ref_img_pts,
            cam_img_pts[cam_idx - 1],
            ref_intr["K"], ref_intr["dist"],
            cam_intr["K"], cam_intr["dist"],
            ref_intr["size"],
            flags=cv2.CALIB_FIX_INTRINSIC,
        )
        transforms[names[cam_idx]] = {"R": R.tolist(), "T": T.tolist()}
        reproj_errors[names[cam_idx]] = round(float(ret), 4)

    # 기준 카메라도 포함 (identity)
    transforms[names[0]] = {
        "R": np.eye(3).tolist(),
        "T": np.zeros((3, 1)).tolist(),
    }

    # intrinsics — K(3×3), dist, 해상도
    intr_out: dict[str, dict] = {}
    for cam_idx, name in enumerate(names):
        K    = intrinsics[cam_idx]["K"]
        dist = intrinsics[cam_idx]["dist"]
        w, h = intrinsics[cam_idx]["size"]
        intr_out[name] = {
            "fx": float(K[0, 0]), "fy": float(K[1, 1]),
            "cx": float(K[0, 2]), "cy": float(K[1, 2]),
            "dist": dist.flatten().tolist(),
            "width": w, "height": h,
        }

    return {
        "ok": True,
        "method": "charuco",
        "cameras": names,
        "transforms": transforms,
        "intrinsics": intr_out,
        "reprojectionErrors": reproj_errors,
    }
