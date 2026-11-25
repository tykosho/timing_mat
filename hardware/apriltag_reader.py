"""Optional optical AprilTag (tag16h5) webcam reader for sensor fusion with RFID.

Gracefully degrades to APRILTAG_AVAILABLE = False when OpenCV / pupil-apriltags are not
installed (e.g., lean Raspberry Pi RFID Docker images or CI). When installed, enumerates
macOS AVFoundation / USB webcams, supports live hot-swapping, draws optional debug overlays,
and emits TagRead(source="apriltag", apriltag_id=...) into the unified LapEngine.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import clock
from core.models import TagRead, apriltag_to_epc
from hardware.base import BaseRFIDReader

logger = logging.getLogger("AprilTagReader")

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]

try:
    import pupil_apriltags
except ImportError:
    pupil_apriltags = None  # type: ignore[assignment]

APRILTAG_AVAILABLE = cv2 is not None and (
    pupil_apriltags is not None or (hasattr(cv2, "aruco") and hasattr(cv2.aruco, "DICT_APRILTAG_16h5"))
)


def _list_macos_cameras() -> Optional[List[Dict[str, Any]]]:
    """Enumerate cameras via AVFoundation sorted by uniqueID (matches OpenCV CAP_AVFOUNDATION)."""
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")

        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        def msg(obj: int, sel_name: bytes, *args: Any, restype: Any = ctypes.c_void_p, argtypes: Optional[List[Any]] = None) -> Any:
            fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *(argtypes or []))(
                ("objc_msgSend", objc)
            )
            return fn(obj, objc.sel_registerName(sel_name), *args)

        ns_string_cls = objc.objc_getClass(b"NSString")
        av_device_cls = objc.objc_getClass(b"AVCaptureDevice")
        if not ns_string_cls or not av_device_cls:
            return None

        media_type = msg(
            ns_string_cls,
            b"stringWithUTF8String:",
            b"vide",
            argtypes=[ctypes.c_char_p],
        )
        devices = msg(
            av_device_cls,
            b"devicesWithMediaType:",
            media_type,
            argtypes=[ctypes.c_void_p],
        )
        if not devices:
            return []

        count = int(msg(devices, b"count", restype=ctypes.c_ulong))
        found = []
        for i in range(count):
            dev = msg(devices, b"objectAtIndex:", i, argtypes=[ctypes.c_ulong])
            uid_obj = msg(dev, b"uniqueID")
            name_obj = msg(dev, b"localizedName")
            uid_bytes = msg(uid_obj, b"UTF8String", restype=ctypes.c_char_p)
            name_bytes = msg(name_obj, b"UTF8String", restype=ctypes.c_char_p)
            found.append(
                {
                    "unique_id": (uid_bytes or b"").decode("utf-8", errors="replace"),
                    "name": (name_bytes or b"Camera").decode("utf-8", errors="replace"),
                }
            )
        found.sort(key=lambda item: item["unique_id"])
        return [{"index": idx, "name": item["name"]} for idx, item in enumerate(found)]
    except Exception:
        return None


def list_available_cameras(max_probe: int = 5) -> List[Dict[str, Any]]:
    """Return available camera devices as [{'index': int, 'name': str}, ...]."""
    if not APRILTAG_AVAILABLE:
        return []
    if sys.platform == "darwin":
        mac_cams = _list_macos_cameras()
        if mac_cams is not None:
            return mac_cams
    cameras: List[Dict[str, Any]] = []
    for idx in range(max_probe):
        cap = cv2.VideoCapture(idx)
        if cap is not None and cap.isOpened():
            cameras.append({"index": idx, "name": f"Camera #{idx}"})
            cap.release()
    return cameras


class AprilTagCameraReader(BaseRFIDReader):
    """Optical AprilTag (tag16h5) reader feeding unified TagReads into LapEngine."""

    def __init__(
        self,
        on_tag_read: Callable[[TagRead], None],
        camera_index: int = 0,
        decision_margin_min: float = 42.0,
        min_tag_area_ratio: float = 0.0012,
    ) -> None:
        super().__init__(on_tag_read)
        self.camera_index = int(camera_index)
        self.camera_name = f"Camera #{self.camera_index}"
        self.decision_margin_min = float(decision_margin_min)
        self.min_tag_area_ratio = float(min_tag_area_ratio)
        self.finish_line_x: float = 0.5
        self.finish_zone_half_width: float = 0.16
        self.debug_overlay: bool = True
        self.mirror: bool = False

        self._thread: Optional[threading.Thread] = None
        self._cap: Optional[Any] = None
        self._cap_index: Optional[int] = None
        self._frame_cond = threading.Condition()
        self._latest_jpeg: bytes = b""
        self._latest_seq: int = 0
        self.fps: float = 0.0
        self.last_error: Optional[str] = None
        self._prev_norm_x: Dict[int, Tuple[float, float]] = {}

    def start(self) -> None:
        if not APRILTAG_AVAILABLE:
            self.last_error = "OpenCV / pupil-apriltags not installed"
            return
        if self.is_running:
            return
        self.is_running = True
        self.last_error = None
        self._thread = threading.Thread(
            target=self._run_loop, name="UnifiedAprilTagReader", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.is_running = False
        with self._frame_cond:
            self._frame_cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
            self._cap_index = None

    def set_config(
        self,
        enabled: Optional[bool] = None,
        camera_index: Optional[int] = None,
        debug_overlay: Optional[bool] = None,
        mirror: Optional[bool] = None,
        finish_line_x: Optional[float] = None,
    ) -> Dict[str, Any]:
        if camera_index is not None:
            self.camera_index = max(0, int(camera_index))
        if debug_overlay is not None:
            self.debug_overlay = bool(debug_overlay)
        if mirror is not None:
            self.mirror = bool(mirror)
        if finish_line_x is not None:
            self.finish_line_x = max(0.1, min(0.9, float(finish_line_x)))
        if enabled is True and not self.is_running:
            self.start()
        elif enabled is False and self.is_running:
            self.stop()
        return self.health()

    def wait_for_jpeg(self, last_seq: int, timeout: float = 0.4) -> Tuple[int, bytes]:
        with self._frame_cond:
            if self._latest_seq == last_seq:
                self._frame_cond.wait(timeout=timeout)
            return self._latest_seq, self._latest_jpeg

    def health(self) -> Dict[str, Any]:
        return {
            "kind": "AprilTagCameraReader",
            "available": APRILTAG_AVAILABLE,
            "running": self.is_running,
            "connected": self.is_running and self._cap is not None,
            "camera_index": self.camera_index,
            "camera_name": self.camera_name,
            "fps": round(self.fps, 1),
            "debug_overlay": self.debug_overlay,
            "mirror": self.mirror,
            "finish_line_x": self.finish_line_x,
            "last_error": self.last_error,
        }

    def _open_camera(self, idx: int) -> Optional[Any]:
        backend = cv2.CAP_AVFOUNDATION if hasattr(cv2, "CAP_AVFOUNDATION") else cv2.CAP_ANY
        cap = cv2.VideoCapture(idx, backend)
        if not cap or not cap.isOpened():
            if cap:
                cap.release()
            self.last_error = f"Could not open camera #{idx}"
            return None
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if hasattr(cv2, "CAP_PROP_CONVERT_RGB"):
            cap.set(cv2.CAP_PROP_CONVERT_RGB, 1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        for _ in range(3):
            cap.read()
        cams = {c["index"]: c["name"] for c in list_available_cameras()}
        self.camera_name = cams.get(idx, f"Camera #{idx}")
        self.last_error = None
        return cap

    def _in_finish_zone_or_crossed(self, tag_id: int, norm_x: float, now: float) -> bool:
        prev = self._prev_norm_x.get(tag_id)
        self._prev_norm_x[tag_id] = (norm_x, now)
        if abs(norm_x - self.finish_line_x) <= self.finish_zone_half_width:
            return True
        if prev is not None and (now - prev[1]) <= 1.8:
            if (prev[0] < self.finish_line_x <= norm_x) or (prev[0] > self.finish_line_x >= norm_x):
                return True
        return False

    def _run_loop(self) -> None:
        detector = None
        aruco_det = None
        if pupil_apriltags is not None:
            detector = pupil_apriltags.Detector(
                families="tag16h5", nthreads=4, quad_decimate=2.0, quad_sigma=0.4, refine_edges=1
            )
        else:
            d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_16h5)
            p = cv2.aruco.DetectorParameters()
            aruco_det = cv2.aruco.ArucoDetector(d, p)

        prev_tick = time.perf_counter()
        smoothed_fps = 24.0
        retry_after = 0.0

        while self.is_running:
            now_wall = time.time()
            desired_idx = self.camera_index
            if self._cap is not None and self._cap_index != desired_idx:
                self._cap.release()
                self._cap = None
                self._cap_index = None

            if self._cap is None and now_wall >= retry_after:
                self._cap = self._open_camera(desired_idx)
                if self._cap is not None:
                    self._cap_index = desired_idx
                else:
                    retry_after = now_wall + 3.0
                    time.sleep(0.2)
                    continue

            if self._cap is None:
                time.sleep(0.1)
                continue

            ok, grabbed = self._cap.read()
            if not ok or grabbed is None or grabbed.ndim != 3 or grabbed.size == 0:
                self._cap.release()
                self._cap = None
                self._cap_index = None
                retry_after = now_wall + 2.0
                continue

            if grabbed.shape[2] == 4:
                grabbed = cv2.cvtColor(grabbed, cv2.COLOR_BGRA2BGR)
            raw_bgr = np.ascontiguousarray(grabbed, dtype=np.uint8)
            h, w = raw_bgr.shape[:2]
            gray = cv2.cvtColor(raw_bgr, cv2.COLOR_BGR2GRAY)
            min_area = float(w * h) * self.min_tag_area_ratio

            accepted: List[Tuple[int, float, Any, float]] = []
            if detector is not None:
                for det in detector.detect(gray):
                    tid = int(det.tag_id)
                    margin = float(det.decision_margin)
                    hamming = int(getattr(det, "hamming", 0))
                    pts = np.array(det.corners, dtype=np.float32)
                    area = float(cv2.contourArea(pts))
                    _, _, bw, bh = cv2.boundingRect(pts.astype(np.int32))
                    aspect = max(bw, bh) / max(1.0, float(min(bw, bh)))
                    if 0 <= tid < 30 and hamming == 0 and margin >= self.decision_margin_min and area >= min_area and aspect <= 3.2:
                        accepted.append((tid, margin, pts, float(det.center[0]) / max(1.0, float(w))))
            elif aruco_det is not None:
                corners_list, ids, _ = aruco_det.detectMarkers(gray)
                if ids is not None:
                    for raw_c, raw_id in zip(corners_list, ids.flatten()):
                        tid = int(raw_id)
                        pts = np.array(raw_c[0], dtype=np.float32)
                        area = float(cv2.contourArea(pts))
                        if 0 <= tid < 30 and area >= min_area:
                            cx = float(np.mean(pts[:, 0])) / max(1.0, float(w))
                            accepted.append((tid, 65.0, pts, cx))

            # Emit unified TagRead for every tag crossing/in the virtual finish zone
            race_now = clock.now()
            for tid, margin, _pts, norm_x in accepted:
                if self._in_finish_zone_or_crossed(tid, norm_x, now_wall):
                    # Map decision_margin (~42..120) to a strong pseudo-RSSI (-55..-30 dBm)
                    pseudo_rssi = min(-30.0, max(-58.0, -65.0 + (margin * 0.3)))
                    self.on_tag_read(
                        TagRead(
                            epc=apriltag_to_epc(tid),
                            timestamp=race_now,
                            rssi=round(pseudo_rssi, 1),
                            source="apriltag",
                            apriltag_id=tid,
                        )
                    )

            curr_tick = time.perf_counter()
            dt = max(1e-3, curr_tick - prev_tick)
            prev_tick = curr_tick
            smoothed_fps = 0.85 * smoothed_fps + 0.15 * (1.0 / dt)
            self.fps = round(smoothed_fps, 1)

            # Render MJPEG preview frame
            out = cv2.flip(raw_bgr, 1) if self.mirror else raw_bgr
            if self.debug_overlay:
                if out is raw_bgr:
                    out = raw_bgr.copy()
                line_norm = (1.0 - self.finish_line_x) if self.mirror else self.finish_line_x
                line_px = int(round(line_norm * w))
                for y0 in range(28, h - 28, 28):
                    cv2.line(out, (line_px, y0), (line_px, min(h - 28, y0 + 16)), (238, 211, 34), 2, cv2.LINE_AA)
                for tid, margin, pts, _nx in accepted:
                    mapped = np.array(
                        [[int(round((w - 1 - p[0]) if self.mirror else p[0])), int(round(p[1]))] for p in pts],
                        dtype=np.int32,
                    )
                    cv2.polylines(out, [mapped], True, (80, 245, 110), 3, cv2.LINE_AA)
                    bx, by = int(np.min(mapped[:, 0])), max(22, int(np.min(mapped[:, 1])) - 8)
                    cv2.putText(out, f"AT #{tid:02d} m={margin:.0f}", (bx, by), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (110, 255, 145), 2, cv2.LINE_AA)
                cv2.rectangle(out, (10, 10), (390, 42), (16, 18, 24), -1)
                cv2.putText(out, f"CAM: {self.camera_name[:22]} | {self.fps:.1f} FPS", (18, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (226, 232, 240), 1, cv2.LINE_AA)

            ok_jpg, buf = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
            if ok_jpg:
                with self._frame_cond:
                    self._latest_jpeg = buf.tobytes()
                    self._latest_seq += 1
                    self._frame_cond.notify_all()
