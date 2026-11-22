"""OpenCV + pupil-apriltags webcam loop, face cropper, synthetic track mode, and debug overlay."""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np
import pupil_apriltags

from generate_tags import render_tag16h5_image
from tracker import SessionTracker


@dataclass
class TagDetectionResult:
    tag_id: int
    decision_margin: float
    hamming: int
    center: Tuple[float, float]
    corners: np.ndarray  # shape (4, 2)
    bbox: Tuple[int, int, int, int]  # (x, y, w, h)
    area: float
    accepted: bool


class FaceCropper:
    """Locates a runner's face via Haar Cascade with portrait fallback above the chest tag."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cascade = None
        if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data"):
            xml_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
            if os.path.exists(xml_path):
                classifier = cv2.CascadeClassifier(xml_path)
                if not classifier.empty():
                    self._cascade = classifier

    def _detect_face_box(
        self, gray: np.ndarray, roi: Optional[Tuple[int, int, int, int]] = None
    ) -> Optional[Tuple[int, int, int, int]]:
        if self._cascade is None:
            return None
        ox, oy = 0, 0
        sub = gray
        if roi is not None:
            rx, ry, rw, rh = roi
            if rw >= 48 and rh >= 48:
                sub = gray[ry : ry + rh, rx : rx + rw]
                ox, oy = rx, ry
            else:
                return None
        with self._lock:
            faces = self._cascade.detectMultiScale(
                sub,
                scaleFactor=1.12,
                minNeighbors=5,
                minSize=(40, 40),
            )
        if faces is None or len(faces) == 0:
            return None
        best = max(faces, key=lambda rect: int(rect[2]) * int(rect[3]))
        fx, fy, fw, fh = int(best[0]) + ox, int(best[1]) + oy, int(best[2]), int(best[3])
        return (fx, fy, fw, fh)

    def crop_portrait_bgr(
        self,
        bgr_frame: np.ndarray,
        tag_bbox: Optional[Tuple[int, int, int, int]] = None,
        out_size: int = 256,
    ) -> np.ndarray:
        """Crop a 256x256 runner face portrait, falling back to the region above the chest tag."""
        h, w = bgr_frame.shape[:2]
        gray = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2GRAY)

        face_box: Optional[Tuple[int, int, int, int]] = None
        if tag_bbox is not None:
            # Restrict face search strictly to the column above this runner's chest tag
            # so side-by-side pack runners never steal a neighbor's face crop.
            tx, ty, tw, th = tag_bbox
            tcx = tx + tw // 2
            roi_w = max(int(tw * 3.4), int(w * 0.24))
            roi_h = max(int(th * 4.2), int(h * 0.52))
            rx = max(0, min(w - 1, tcx - roi_w // 2))
            ry = max(0, ty - roi_h)
            rw = min(w - rx, roi_w)
            rh = max(0, min(h - ry, ty + th // 2 - ry))
            face_box = self._detect_face_box(gray, (rx, ry, rw, rh))
        else:
            face_box = self._detect_face_box(gray, None)

        if face_box is not None:
            fx, fy, fw, fh = face_box
            side = int(max(fw, fh) * 1.65)
            cx = fx + fw // 2
            cy = fy + int(fh * 0.48)
        elif tag_bbox is not None:
            tx, ty, tw, th = tag_bbox
            side = int(max(tw * 2.5, th * 2.5, min(w, h) * 0.24))
            cx = tx + tw // 2
            cy = ty - int(side * 0.58)
        else:
            side = int(min(w, h) * 0.42)
            cx = w // 2
            cy = int(h * 0.38)

        side = max(64, min(side, w, h))
        x0 = max(0, min(w - side, cx - side // 2))
        y0 = max(0, min(h - side, cy - side // 2))
        crop = bgr_frame[y0 : y0 + side, x0 : x0 + side]
        return cv2.resize(crop, (out_size, out_size), interpolation=cv2.INTER_AREA)

    def crop_portrait_base64(
        self,
        bgr_frame: np.ndarray,
        tag_bbox: Optional[Tuple[int, int, int, int]] = None,
    ) -> str:
        portrait = self.crop_portrait_bgr(bgr_frame, tag_bbox=tag_bbox, out_size=256)
        ok, buf = cv2.imencode(".png", portrait)
        if not ok:
            return ""
        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        return f"data:image/png;base64,{b64}"


def detect_apriltags_16h5(
    detector: pupil_apriltags.Detector,
    gray_frame: np.ndarray,
    decision_margin_min: float = 42.0,
    min_tag_area_ratio: float = 0.0012,
    max_hamming: int = 0,
) -> Tuple[List[TagDetectionResult], List[TagDetectionResult]]:
    """Run tag16h5 detection and split into (accepted, rejected) lists.

    Requires `hamming <= max_hamming` (default 0 bit errors) so motion blur or angled
    tags during walk-up registration cannot flip 1-2 bits into a wrong tag16h5 ID.
    """
    h, w = gray_frame.shape[:2]
    min_area = float(w * h) * float(min_tag_area_ratio)
    raw_detections = detector.detect(gray_frame)

    accepted: List[TagDetectionResult] = []
    rejected: List[TagDetectionResult] = []

    for det in raw_detections:
        tid = int(det.tag_id)
        margin = float(det.decision_margin)
        hamming = int(getattr(det, "hamming", 0))
        corners = np.array(det.corners, dtype=np.float32)
        area = float(cv2.contourArea(corners))
        bx, by, bw, bh = cv2.boundingRect(corners.astype(np.int32))
        cx, cy = float(det.center[0]), float(det.center[1])

        # Reject extreme sliver aspect ratios (> 3.2:1) caused by steep grazing angles
        aspect = max(bw, bh) / max(1.0, float(min(bw, bh)))
        is_valid = (
            (0 <= tid < 30)
            and (hamming <= max_hamming)
            and (margin >= decision_margin_min)
            and (area >= min_area)
            and (aspect <= 3.2)
        )
        item = TagDetectionResult(
            tag_id=tid,
            decision_margin=margin,
            hamming=hamming,
            center=(cx, cy),
            corners=corners,
            bbox=(int(bx), int(by), int(bw), int(bh)),
            area=area,
            accepted=is_valid,
        )
        if is_valid:
            accepted.append(item)
        else:
            rejected.append(item)

    return accepted, rejected


@dataclass
class VirtualRunnerPass:
    tag_id: int
    started_at: float
    duration: float = 2.6
    y_lane: int = 410
    x_stagger: int = 0


class SyntheticTrackScene:
    """Generates a 1280x720 trackside scene with virtual runners wearing real tag16h5 bibs."""

    def __init__(self, width: int = 1280, height: int = 720) -> None:
        self.width = width
        self.height = height
        self._lock = threading.Lock()
        self._passes: List[VirtualRunnerPass] = []
        self._tag_cache: Dict[int, np.ndarray] = {}
        self._bg = self._build_background()

    def _build_background(self) -> np.ndarray:
        bg = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        # Dusk stadium gradient sky
        for y in range(260):
            ratio = y / 260.0
            bg[y, :] = (
                int(38 + 25 * ratio),
                int(26 + 20 * ratio),
                int(20 + 18 * ratio),
            )
        # Infield turf
        bg[260:330, :] = (36, 72, 34)
        # Polyurethane running track surface
        bg[330:, :] = (46, 58, 138)
        for lane_y in (330, 415, 510, 610, 712):
            cv2.line(bg, (0, lane_y), (self.width, lane_y), (235, 235, 240), 2, cv2.LINE_AA)
        return bg

    def _tag_patch_bgr(self, tag_id: int, tag_px: int = 96) -> np.ndarray:
        key = tag_id * 1000 + tag_px
        cached = self._tag_cache.get(key)
        if cached is not None:
            return cached
        gray = render_tag16h5_image(tag_id, tag_px=tag_px, border_cells=1)
        bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        self._tag_cache[key] = bgr
        return bgr

    def spawn_runner_pass(
        self, tag_id: int, duration: float = 2.4, slot: Optional[int] = None
    ) -> None:
        lane_slots = (
            (390, -110),
            (500, 110),
            (590, 0),
            (445, -220),
        )
        with self._lock:
            idx = (len(self._passes) if slot is None else slot) % len(lane_slots)
            lane_y, x_stag = lane_slots[idx]
            self._passes.append(
                VirtualRunnerPass(
                    tag_id=int(tag_id) % 30,
                    started_at=time.time(),
                    duration=duration,
                    y_lane=lane_y,
                    x_stagger=x_stag,
                )
            )

    def render_runner_on_frame(
        self, frame: np.ndarray, tag_id: int, center_x: int, chest_y: int
    ) -> Tuple[int, int, int, int]:
        """Draw a stylized runner + real tag16h5 bib onto `frame`; return tag bbox."""
        hue = (tag_id * 41) % 180
        jersey_bgr = tuple(
            int(c)
            for c in cv2.cvtColor(np.uint8([[[hue, 185, 195]]]), cv2.COLOR_HSV2BGR)[0, 0]
        )
        skin_tones = [(145, 175, 215), (95, 130, 175), (170, 198, 232), (75, 102, 142)]
        skin = skin_tones[tag_id % len(skin_tones)]

        # Torso / shoulders
        cv2.ellipse(frame, (center_x, chest_y + 24), (82, 112), 0, 0, 360, jersey_bgr, -1, cv2.LINE_AA)
        # Neck & Head above chest bib (for realistic fallback/portrait crop)
        head_y = chest_y - 126
        cv2.rectangle(frame, (center_x - 16, head_y + 34), (center_x + 16, chest_y - 68), skin, -1)
        cv2.circle(frame, (center_x, head_y), 46, skin, -1, cv2.LINE_AA)
        # Hair, eyes, smile
        cv2.ellipse(frame, (center_x, head_y - 14), (48, 34), 0, 180, 360, (32, 32, 38), -1, cv2.LINE_AA)
        cv2.circle(frame, (center_x - 15, head_y - 4), 5, (24, 24, 28), -1, cv2.LINE_AA)
        cv2.circle(frame, (center_x + 15, head_y - 4), 5, (24, 24, 28), -1, cv2.LINE_AA)
        cv2.ellipse(frame, (center_x, head_y + 12), (16, 9), 0, 15, 165, (35, 40, 90), 2, cv2.LINE_AA)

        # Crisp AprilTag 16h5 chest bib
        bib = self._tag_patch_bgr(tag_id, tag_px=96)
        bh, bw = bib.shape[:2]
        bx = max(0, min(self.width - bw, center_x - bw // 2))
        by = max(0, min(self.height - bh, chest_y - bh // 2))
        frame[by : by + bh, bx : bx + bw] = bib
        return (bx, by, bw, bh)

    def render_frame(self, now: Optional[float] = None) -> np.ndarray:
        ts = time.time() if now is None else now
        frame = self._bg.copy()
        with self._lock:
            active: List[VirtualRunnerPass] = []
            for p in self._passes:
                prog = (ts - p.started_at) / max(0.3, p.duration)
                if 0.0 <= prog <= 1.0:
                    active.append(p)
                    cx = max(85, min(self.width - 85, int(110 + prog * (self.width - 220)) + p.x_stagger))
                    bob = int(6 * np.sin(prog * np.pi * 8))
                    self.render_runner_on_frame(frame, p.tag_id, cx, p.y_lane + bob)
            self._passes = active

        if not active:
            cv2.putText(
                frame,
                "SIMULATED TRACK CAMERA  |  Press [U] Unknown Bib  or  [L] Simulate Lap",
                (44, self.height - 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.68,
                (215, 225, 235),
                2,
                cv2.LINE_AA,
            )
        return frame


def draw_debug_overlays(
    raw_bgr: np.ndarray,
    accepted: List[TagDetectionResult],
    tracker: SessionTracker,
    fps: float,
    exposure: float,
    camera_source: str,
    now: Optional[float] = None,
) -> np.ndarray:
    """Render video frame (applying mirror if toggled) and draw clean debug HUD & tag overlays."""
    ts = time.time() if now is None else now
    mirror = tracker.mirror
    debug_on = tracker.debug_overlay
    out = cv2.flip(raw_bgr, 1) if mirror else raw_bgr.copy()
    if not debug_on:
        return out

    h, w = out.shape[:2]

    def map_x(x_val: float) -> int:
        return int(round((w - 1 - x_val) if mirror else x_val))

    # 1. Virtual finish line + non-intrusive top/bottom zone brackets (keeps center camera image clean)
    line_norm = (1.0 - tracker.finish_line_x) if mirror else tracker.finish_line_x
    zone_half = tracker.finish_zone_half_width
    line_px = int(round(line_norm * w))
    z_left = max(0, int(round((line_norm - zone_half) * w)))
    z_right = min(w - 1, int(round((line_norm + zone_half) * w)))

    # Subtle top & bottom trigger-zone header/footer bands instead of tinting the full frame center
    band_h = 36
    zone_overlay = out.copy()
    cv2.rectangle(zone_overlay, (z_left, 0), (z_right, band_h), (210, 190, 30), -1)
    cv2.rectangle(zone_overlay, (z_left, h - band_h), (z_right, h), (210, 190, 30), -1)
    cv2.addWeighted(zone_overlay, 0.25, out, 0.75, 0, out)

    # Corner tick brackets for active trigger zone boundaries
    for zx in (z_left, z_right):
        cv2.line(out, (zx, 0), (zx, band_h), (210, 210, 70), 2, cv2.LINE_AA)
        cv2.line(out, (zx, h - band_h), (zx, h), (210, 210, 70), 2, cv2.LINE_AA)

    # Dashed center finish line so faces behind the line remain clear
    dash_len, gap_len = 18, 12
    for y0 in range(band_h, h - band_h, dash_len + gap_len):
        y1 = min(h - band_h, y0 + dash_len)
        cv2.line(out, (line_px, y0), (line_px, y1), (238, 211, 34), 2, cv2.LINE_AA)

    fl_label = "FINISH LINE"
    cv2.rectangle(out, (max(4, line_px - 64), 6), (min(w - 4, line_px + 64), 32), (20, 22, 28), -1)
    cv2.putText(
        out,
        fl_label,
        (max(10, line_px - 54), 25),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        (238, 211, 34),
        2,
        cv2.LINE_AA,
    )

    # 2. Accepted detections ONLY (no low-margin rejected background noise polygons)
    for det in accepted:
        pts = np.array([[map_x(pt[0]), int(round(pt[1]))] for pt in det.corners], dtype=np.int32)
        cv2.polylines(out, [pts], isClosed=True, color=(80, 245, 110), thickness=3, lineType=cv2.LINE_AA)
        for px, py in pts:
            cv2.circle(out, (int(px), int(py)), 5, (34, 211, 238), -1, cv2.LINE_AA)

        bx_min = int(np.min(pts[:, 0]))
        by_min = int(np.min(pts[:, 1]))
        by_max = int(np.max(pts[:, 1]))

        runner = tracker.runners.get(det.tag_id)
        name_part = runner.name if runner else ("IGNORED" if det.tag_id in tracker.ignored_tags else "NEW TAG")
        tag_label = f"ID {det.tag_id:02d} ({name_part}) | margin {det.decision_margin:.1f}"

        (tw_txt, th_txt), _ = cv2.getTextSize(tag_label, cv2.FONT_HERSHEY_SIMPLEX, 0.56, 2)
        lbl_y = min(h - 10, by_max + th_txt + 14)
        cv2.rectangle(
            out,
            (max(2, bx_min - 4), lbl_y - th_txt - 6),
            (min(w - 2, bx_min + tw_txt + 8), lbl_y + 6),
            (18, 28, 22),
            -1,
        )
        cv2.putText(
            out,
            tag_label,
            (max(6, bx_min), lbl_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.56,
            (110, 255, 145),
            2,
            cv2.LINE_AA,
        )

        # Cooldown countdown badge hovering above recently counted tags
        if runner is not None:
            cd_rem = runner.cooldown_remaining(ts)
            if cd_rem > 0.0:
                cd_text = f"COOLDOWN: {int(np.ceil(cd_rem))}s"
                pill_bg = (16, 115, 225)  # BGR amber/orange
                pill_fg = (255, 255, 255)
            else:
                cd_text = f"LAPS: {runner.laps} | READY"
                pill_bg = (55, 155, 45)
                pill_fg = (245, 255, 245)

            (cw_txt, ch_txt), _ = cv2.getTextSize(cd_text, cv2.FONT_HERSHEY_SIMPLEX, 0.58, 2)
            top_y = max(ch_txt + 14, by_min - 12)
            cv2.rectangle(
                out,
                (max(2, bx_min - 4), top_y - ch_txt - 8),
                (min(w - 2, bx_min + cw_txt + 10), top_y + 6),
                pill_bg,
                -1,
            )
            cv2.putText(
                out,
                cd_text,
                (max(7, bx_min + 2), top_y - 1),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.58,
                pill_fg,
                2,
                cv2.LINE_AA,
            )

    # 4. Top-left telemetry HUD
    hud_lines = [
        f"FPS: {fps:4.1f} | {w}x{h} | EXP: {exposure:.1f}",
        f"SRC: {camera_source} | tag16h5 (m>={tracker.decision_margin_min:.0f})",
    ]
    cv2.rectangle(out, (12, 12), (432, 68), (16, 18, 24), -1)
    cv2.rectangle(out, (12, 12), (432, 68), (65, 75, 90), 1)
    for idx, line in enumerate(hud_lines):
        cv2.putText(
            out,
            line,
            (20, 34 + idx * 23),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.50,
            (226, 232, 240),
            1,
            cv2.LINE_AA,
        )

    return out


def _list_macos_avfoundation_cameras() -> Optional[List[Dict[str, Any]]]:
    """Enumerate macOS video capture devices in OpenCV AVFoundation index order (sorted by uniqueID)."""
    if sys.platform != "darwin":
        return None
    try:
        objc_path = ctypes.util.find_library("objc")
        if not objc_path:
            return None
        objc = ctypes.cdll.LoadLibrary(objc_path)
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")

        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        def msg(
            obj: Any,
            sel_name: bytes,
            *args: Any,
            restype: Any = ctypes.c_void_p,
            argtypes: Tuple[Any, ...] = (),
        ) -> Any:
            fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(
                ("objc_msgSend", objc)
            )
            return fn(obj, objc.sel_registerName(sel_name), *args)

        ns_string = objc.objc_getClass(b"NSString")
        media_video = msg(ns_string, b"stringWithUTF8String:", b"vide", argtypes=(ctypes.c_char_p,))
        av_capture_device = objc.objc_getClass(b"AVCaptureDevice")
        devices = msg(
            av_capture_device, b"devicesWithMediaType:", media_video, argtypes=(ctypes.c_void_p,)
        )
        if not devices:
            return []
        count = int(msg(devices, b"count", restype=ctypes.c_ulong))
        raw_devices: List[Dict[str, str]] = []
        for i in range(count):
            dev = msg(devices, b"objectAtIndex:", i, argtypes=(ctypes.c_ulong,))
            name_ns = msg(dev, b"localizedName")
            uid_ns = msg(dev, b"uniqueID")
            name_bytes = msg(name_ns, b"UTF8String", restype=ctypes.c_char_p) if name_ns else None
            uid_bytes = msg(uid_ns, b"UTF8String", restype=ctypes.c_char_p) if uid_ns else None
            name = name_bytes.decode("utf-8", errors="replace") if name_bytes else f"Camera #{i}"
            uid = uid_bytes.decode("utf-8", errors="replace") if uid_bytes else ""
            raw_devices.append({"name": name, "unique_id": uid})

        # OpenCV's cap_avfoundation_mac.mm sorts capture devices ascending by uniqueID
        raw_devices.sort(key=lambda item: item["unique_id"])
        return [
            {"index": idx, "name": item["name"], "unique_id": item["unique_id"]}
            for idx, item in enumerate(raw_devices)
        ]
    except Exception:
        return None


def list_available_cameras(max_probe: int = 6) -> List[Dict[str, Any]]:
    """Return available camera devices with OpenCV-aligned indices and human-readable names."""
    mac_cams = _list_macos_avfoundation_cameras()
    if mac_cams is not None:
        return mac_cams

    found: List[Dict[str, Any]] = []
    backend = cv2.CAP_AVFOUNDATION if hasattr(cv2, "CAP_AVFOUNDATION") else cv2.CAP_ANY
    consecutive_misses = 0
    for idx in range(max(1, max_probe)):
        cap = cv2.VideoCapture(idx, backend)
        if cap and cap.isOpened():
            found.append({"index": idx, "name": f"Camera #{idx}"})
            cap.release()
            consecutive_misses = 0
        else:
            if cap:
                cap.release()
            consecutive_misses += 1
            if found and consecutive_misses >= 2:
                break
    return found


class VisionEngine:
    """Background webcam + AprilTag CV engine streaming MJPEG frames and emitting events."""

    def __init__(
        self,
        tracker: SessionTracker,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        camera_index: int = 0,
        force_simulated_camera: bool = False,
        camera_lister: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    ) -> None:
        self.tracker = tracker
        self.on_event = on_event
        self.camera_index = int(camera_index)
        self.force_simulated_camera = force_simulated_camera
        self._camera_lister = camera_lister
        self._camera_names: Dict[int, str] = {}
        self._active_camera_index: Optional[int] = None
        self._last_attempted_camera_index: Optional[int] = None

        self.face_cropper = FaceCropper()
        self.synth_scene = SyntheticTrackScene(1280, 720)
        self._detector: Optional[pupil_apriltags.Detector] = None

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._cap: Optional[cv2.VideoCapture] = None

        self._frame_cond = threading.Condition()
        self._latest_raw_bgr: np.ndarray = self.synth_scene.render_frame()
        self._latest_jpeg: bytes = self._encode_jpeg(self._latest_raw_bgr)
        self._latest_seq: int = 0
        self._latest_bboxes: Dict[int, Tuple[int, int, int, int]] = {}
        self._latest_margins: Dict[int, float] = {}
        # Multi-frame confirmation tracker for unknown tags:
        # tag_id -> {"hits": [timestamps], "best_margin": float, "best_b64": str}
        self._unknown_sightings: Dict[int, Dict[str, Any]] = {}
        self.fps: float = 0.0
        self.exposure: float = 0.0
        self.camera_source: str = "INIT"

    def list_cameras(self) -> List[Dict[str, Any]]:
        """Discover available cameras and cache their human-readable names."""
        if self._camera_lister is not None:
            cams = list(self._camera_lister())
        elif self.force_simulated_camera:
            cams = []
        else:
            cams = list_available_cameras()
        self._camera_names = {int(c["index"]): str(c["name"]) for c in cams if "index" in c}
        return cams

    def _format_camera_source(self, index: int) -> str:
        name = self._camera_names.get(index)
        if name:
            return f"CAM #{index}: {name}"
        return f"WEBCAM #{index}"

    @staticmethod
    def _encode_jpeg(bgr: np.ndarray, quality: int = 88) -> bytes:
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buf.tobytes() if ok else b""

    def _emit(self, payload: Dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(payload)
            except Exception:
                pass

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="AprilTagVisionLoop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
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

    def _open_webcam(self, index: Optional[int] = None) -> Optional[cv2.VideoCapture]:
        if self.force_simulated_camera or self.tracker.simulated_mode:
            return None
        cam_idx = self.camera_index if index is None else int(index)
        self._last_attempted_camera_index = cam_idx
        backend = cv2.CAP_AVFOUNDATION if hasattr(cv2, "CAP_AVFOUNDATION") else cv2.CAP_ANY
        cap = cv2.VideoCapture(cam_idx, backend)
        if not cap or not cap.isOpened():
            if cap:
                cap.release()
            return None
        if hasattr(cv2, "CAP_PROP_CONVERT_RGB"):
            cap.set(cv2.CAP_PROP_CONVERT_RGB, 1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        # Discard initial AVFoundation auto-exposure / stride warmup frames
        for _ in range(3):
            cap.read()
        self.exposure = float(cap.get(cv2.CAP_PROP_EXPOSURE) or 0.0)
        self.camera_index = cam_idx
        self._active_camera_index = cam_idx
        if cam_idx not in self._camera_names:
            self.list_cameras()
        return cap

    def snap_face_base64(self, tag_id: Optional[int] = None) -> str:
        """Snap a fresh face portrait from the most recent raw camera frame."""
        with self._frame_cond:
            frame = self._latest_raw_bgr.copy()
            bbox = self._latest_bboxes.get(int(tag_id)) if tag_id is not None else None
        return self.face_cropper.crop_portrait_base64(frame, tag_bbox=bbox)

    def rescan_live_tag(self) -> Optional[Dict[str, Any]]:
        """Return the highest-confidence tag currently visible in the live frame plus face crop."""
        with self._frame_cond:
            if not self._latest_margins:
                return None
            best_tid = max(self._latest_margins.items(), key=lambda kv: kv[1])[0]
            margin = self._latest_margins[best_tid]
            frame = self._latest_raw_bgr.copy()
            bbox = self._latest_bboxes.get(best_tid)
        preview = self.face_cropper.crop_portrait_base64(frame, tag_bbox=bbox)
        return {
            "tag_id": int(best_tid),
            "decision_margin": round(float(margin), 1),
            "preview_image_base64": preview,
        }

    def render_synthetic_tag_portrait_base64(self, tag_id: int) -> str:
        """Render a synthetic runner wearing `tag_id` and run the real face/fallback cropper."""
        canvas = self.synth_scene._bg.copy()
        bbox = self.synth_scene.render_runner_on_frame(canvas, tag_id=tag_id, center_x=640, chest_y=455)
        return self.face_cropper.crop_portrait_base64(canvas, tag_bbox=bbox)

    def wait_for_jpeg(self, last_seq: int, timeout: float = 0.5) -> Tuple[int, bytes]:
        with self._frame_cond:
            if self._latest_seq == last_seq:
                self._frame_cond.wait(timeout=timeout)
            return self._latest_seq, self._latest_jpeg

    def _run_loop(self) -> None:
        detector = pupil_apriltags.Detector(
            families="tag16h5",
            nthreads=2,
            quad_decimate=1.0,
            quad_sigma=0.8,
            refine_edges=1,
        )
        self._detector = detector
        prev_tick = time.perf_counter()
        smoothed_fps = 28.0
        webcam_retry_after = 0.0
        frame_counter = 0

        while self._running:
            now_wall = time.time()
            frame_counter += 1
            want_sim = self.force_simulated_camera or self.tracker.simulated_mode
            target_idx = int(self.tracker.camera_index)
            self.camera_index = target_idx

            if want_sim and self._cap is not None:
                self._cap.release()
                self._cap = None
                self._active_camera_index = None
            elif not want_sim:
                if self._cap is not None and self._active_camera_index != target_idx:
                    self._cap.release()
                    self._cap = None
                    self._active_camera_index = None
                    webcam_retry_after = 0.0
                if self._cap is None and (
                    now_wall >= webcam_retry_after
                    or self._last_attempted_camera_index != target_idx
                ):
                    self._cap = self._open_webcam(target_idx)
                    if self._cap is None:
                        webcam_retry_after = now_wall + 4.0

            raw_bgr: Optional[np.ndarray] = None
            if self._cap is not None:
                ok, grabbed = self._cap.read()
                if ok and grabbed is not None and grabbed.ndim == 3 and grabbed.size > 0:
                    if grabbed.shape[2] == 4:
                        grabbed = cv2.cvtColor(grabbed, cv2.COLOR_BGRA2BGR)
                    raw_bgr = np.ascontiguousarray(grabbed, dtype=np.uint8)
                    self.camera_source = self._format_camera_source(self.camera_index)
                    if frame_counter % 60 == 0:
                        self.exposure = float(self._cap.get(cv2.CAP_PROP_EXPOSURE) or 0.0)
                else:
                    self._cap.release()
                    self._cap = None
                    self._active_camera_index = None
                    webcam_retry_after = now_wall + 3.0

            if raw_bgr is None:
                raw_bgr = self.synth_scene.render_frame(now_wall)
                self.camera_source = "SIMULATOR"
                self.exposure = -5.0

            gray = cv2.cvtColor(raw_bgr, cv2.COLOR_BGR2GRAY)
            accepted, _rejected = detect_apriltags_16h5(
                detector,
                gray,
                decision_margin_min=self.tracker.decision_margin_min,
                min_tag_area_ratio=self.tracker.min_tag_area_ratio,
            )

            h, w = raw_bgr.shape[:2]
            current_bboxes: Dict[int, Tuple[int, int, int, int]] = {}
            current_margins: Dict[int, float] = {}
            state_dirty = False

            # Prune stale unknown-tag multi-frame sightings (> 1.0s old)
            for utid in list(self._unknown_sightings.keys()):
                entry = self._unknown_sightings[utid]
                entry["hits"] = [t for t in entry["hits"] if now_wall - t <= 1.0]
                if not entry["hits"]:
                    self._unknown_sightings.pop(utid, None)

            # Evaluate EVERY accepted tag in the frame independently so packs count simultaneously
            for det in accepted:
                current_bboxes[det.tag_id] = det.bbox
                current_margins[det.tag_id] = det.decision_margin
                if self.tracker.is_registered(det.tag_id):
                    norm_x = det.center[0] / max(1.0, float(w))
                    if self.tracker.check_line_trigger(det.tag_id, norm_x, now=now_wall):
                        lap_evt = self.tracker.record_crossing(det.tag_id, now=now_wall)
                        if lap_evt is not None:
                            self._emit({"type": "LAP_EVENT", **lap_evt})
                            state_dirty = True
                elif (
                    self.tracker.should_prompt_unknown(det.tag_id)
                    and det.decision_margin >= self.tracker.registration_margin_min
                ):
                    sight = self._unknown_sightings.get(det.tag_id)
                    if sight is None:
                        sight = {"hits": [], "best_margin": -1.0, "best_b64": ""}
                        self._unknown_sightings[det.tag_id] = sight
                    sight["hits"].append(now_wall)
                    if det.decision_margin > sight["best_margin"]:
                        sight["best_margin"] = det.decision_margin
                        sight["best_b64"] = self.face_cropper.crop_portrait_base64(
                            raw_bgr, tag_bbox=det.bbox
                        )
                    if len(sight["hits"]) >= self.tracker.registration_confirm_frames:
                        preview_b64 = sight["best_b64"] or self.face_cropper.crop_portrait_base64(
                            raw_bgr, tag_bbox=det.bbox
                        )
                        self._unknown_sightings.pop(det.tag_id, None)
                        prompt = self.tracker.mark_pending_registration(
                            det.tag_id, preview_b64, now=now_wall
                        )
                        if prompt is not None:
                            self._emit({"type": "REGISTER_PROMPT", **prompt})
                            state_dirty = True

            if state_dirty:
                self._emit({"type": "STATE_SYNC", **self.tracker.get_state(now_wall)})

            curr_tick = time.perf_counter()
            dt = max(1e-3, curr_tick - prev_tick)
            prev_tick = curr_tick
            smoothed_fps = 0.85 * smoothed_fps + 0.15 * (1.0 / dt)
            self.fps = round(smoothed_fps, 1)

            rendered = draw_debug_overlays(
                raw_bgr=raw_bgr,
                accepted=accepted,
                tracker=self.tracker,
                fps=self.fps,
                exposure=self.exposure,
                camera_source=self.camera_source,
                now=now_wall,
            )
            jpeg = self._encode_jpeg(rendered)

            with self._frame_cond:
                self._latest_raw_bgr = raw_bgr
                self._latest_bboxes = current_bboxes
                self._latest_margins = current_margins
                if jpeg:
                    self._latest_jpeg = jpeg
                    self._latest_seq += 1
                self._frame_cond.notify_all()

            # Cap synthetic loop to ~30 FPS so CPU stays cool on MacBook Pro
            if self._cap is None:
                elapsed_loop = time.perf_counter() - curr_tick
                sleep_for = max(0.005, (1.0 / 30.0) - elapsed_loop)
                time.sleep(sleep_for)
