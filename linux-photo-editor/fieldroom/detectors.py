"""Local, CPU-only YuNet/YOLOX inference. This module never downloads anything."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading

import numpy as np

from .models import ModelUnavailable, locate_models


@dataclass(frozen=True)
class Detection:
    kind: str
    box: tuple[float, float, float, float]  # oriented image, normalized l/t/r/b
    confidence: float


_LOCK = threading.RLock()
_ENGINE = None
_ENGINE_KEY = None


def _engine(directory=None):
    global _ENGINE, _ENGINE_KEY
    try:
        import cv2
    except (ImportError, OSError) as exc:
        raise ModelUnavailable("Smart Crop needs its OpenCV runtime. Run the Fieldroom installer again.") from exc
    if not (hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "dnn")):
        raise ModelUnavailable("The installed OpenCV cannot run Smart Crop. Run the updated Fieldroom installer.")
    # locate_models verifies the pinned bytes before they are loaded. Model objects
    # are reused, but a changed installation forces verification and a fresh load.
    paths = locate_models(directory)
    key = tuple((str(paths[k]), paths[k].stat().st_mtime_ns, paths[k].stat().st_size)
                for k in ("face", "person"))
    if _ENGINE is None or key != _ENGINE_KEY:
        try:
            cv2.setNumThreads(2)
            face = cv2.FaceDetectorYN.create(str(paths["face"]), "", (320, 320), .85, .3, 5000)
            person = cv2.dnn.readNetFromONNX(str(paths["person"]))
            person.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            person.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            grids, strides = [], []
            for stride in (8, 16, 32):
                y, x = np.mgrid[:640 // stride, :640 // stride]
                grids.append(np.stack((x, y), axis=-1).reshape(-1, 2))
                strides.append(np.full((x.size, 1), stride, dtype=np.float32))
            _ENGINE = (cv2, face, person, np.concatenate(grids), np.concatenate(strides))
            _ENGINE_KEY = key
        except cv2.error as exc:
            raise ModelUnavailable("Smart Crop could not load its offline models. Run the updated Fieldroom installer.") from exc
    return _ENGINE


def _normalized(kind, xywh, score, width, height):
    x, y, w, h = map(float, xywh)
    if not np.isfinite([x, y, w, h, score]).all() or w <= 0 or h <= 0:
        return None
    box = (max(0., x / width), max(0., y / height),
           min(1., (x + w) / width), min(1., (y + h) / height))
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    return Detection(kind, box, float(score))


def detect_rgb(rgb, directory=None):
    """Find candidate people/faces; policy thresholds are applied by smartcrop.

    RGB is the oriented, neutral developed preview. YOLOX expects 0..255 RGB,
    top-left aligned in a 640 square padded with 114; no mean/std or /255.
    YuNet expects BGR. Serialized access protects the mutable OpenCV networks.
    """
    rgb = np.asarray(rgb, dtype=np.uint8)
    if rgb.ndim != 3 or rgb.shape[2] != 3 or min(rgb.shape[:2]) < 1:
        raise ValueError("Smart Crop needs a nonempty RGB image.")
    height, width = rgb.shape[:2]
    with _LOCK:
        cv2, face_net, person_net, grid, strides = _engine(directory)
        try:
            bgr = np.ascontiguousarray(rgb[:, :, ::-1])
            if min(width, height) < 32:
                bgr = cv2.copyMakeBorder(bgr, 0, max(0, 32-height), 0, max(0, 32-width),
                                         cv2.BORDER_CONSTANT)
            face_net.setInputSize((bgr.shape[1], bgr.shape[0]))
            _, faces = face_net.detect(bgr)
            found = []
            if faces is not None:
                for row in faces:
                    if row[2] < 16 or row[3] < 16:
                        continue
                    item = _normalized("face", row[:4], row[14], width, height)
                    if item:
                        found.append(item)

            scale = min(640 / width, 640 / height)
            resized = cv2.resize(rgb, (max(1, int(width * scale)), max(1, int(height * scale))))
            canvas = np.full((640, 640, 3), 114, dtype=np.uint8)
            canvas[:resized.shape[0], :resized.shape[1]] = resized
            blob = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None], dtype=np.float32)
            person_net.setInput(blob)
            raw = person_net.forward()[0]
            if raw.shape != (8400, 85):
                raise ModelUnavailable("The offline person model has an unexpected format. Run the Fieldroom installer.")
            centers = (raw[:, :2] + grid) * strides
            sizes = np.exp(np.clip(raw[:, 2:4], -10, 10)) * strides
            scores = raw[:, 4] * raw[:, 5]
            keep = (np.argmax(raw[:, 5:], axis=1) == 0) & (scores >= .50)
            keep &= np.isfinite(centers).all(axis=1) & np.isfinite(sizes).all(axis=1) & np.isfinite(scores)
            boxes = np.concatenate((centers[keep] - sizes[keep] / 2, sizes[keep]), axis=1) / scale
            scores = scores[keep]
            if len(boxes):
                indices = cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), .50, .45)
                for i in np.asarray(indices).reshape(-1):
                    item = _normalized("person", boxes[i], scores[i], width, height)
                    if item:
                        found.append(item)
            return tuple(found)
        except cv2.error as exc:
            raise ModelUnavailable("Offline subject detection could not run. Run the updated Fieldroom installer; your crop is unchanged.") from exc


def check_runtime(directory=None):
    """Installer smoke check: verify both models can actually perform inference."""
    detect_rgb(np.zeros((96, 128, 3), dtype=np.uint8), directory=directory)
