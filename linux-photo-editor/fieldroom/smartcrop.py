"""Offline subject detection and conservative crop suggestions.

OpenCV Haar face detection is used for portraits; HOG person detection is used
for full-length framing. No remote inference or network service is involved.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from . import imaging as im

PRESETS = {"very-tight": 1.18, "tight": 1.65, "normal": 2.4, "loose": 3.6, "full-body": 1.14}


def _fit(box, width, height, padding=1.0):
    x, y, w, h = [float(v) for v in box]
    cx, cy = x + w / 2, y + h / 2
    w, h = min(width, w * padding), min(height, h * padding)
    left = max(0., min(width - w, cx - w / 2))
    top = max(0., min(height - h, cy - h / 2))
    return [round(left / width, 6), round(top / height, 6),
            round((left + w) / width, 6), round((top + h) / height, 6)]


def suggest(path, preset="tight"):
    if preset not in PRESETS:
        raise ValueError("Unknown Smart Crop preset.")
    try:
        import cv2
    except ImportError as exc:
        raise ValueError("Offline Smart Crop requires opencv-python-headless.") from exc

    source = im.decode(path, edge=960, thumbnail=False)
    preview = im.render(source, im.recipe())
    rgb = np.asarray(preview.convert("RGB"))
    height, width = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    if preset == "full-body":
        hog = cv2.HOGDescriptor()
        hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        boxes, weights = hog.detectMultiScale(rgb, winStride=(8, 8), padding=(8, 8), scale=1.05)
        if len(boxes):
            order = sorted(range(len(boxes)), key=lambda i: float(weights[i]) * max(1, boxes[i][2] * boxes[i][3]), reverse=True)
            box = boxes[order[0]]
            return {"crop": _fit(box, width, height, PRESETS[preset]), "method": "person",
                    "subjects": len(boxes), "warning": None}
        return {"crop": [0, 0, 1, 1], "method": "none", "subjects": 0,
                "warning": "No full-length person detected. Adjust the crop manually."}

    classifier = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = classifier.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(25, 25))
    if not len(faces):
        return {"crop": [0, 0, 1, 1], "method": "none", "subjects": 0,
                "warning": "No face detected. Adjust the crop manually."}
    # Prefer the largest face; on ties, prefer one nearest the center.
    face = max(faces, key=lambda b: b[2] * b[3] - .08 * ((b[0] + b[2]/2 - width/2)**2 +
                                                         (b[1] + b[3]/2 - height/2)**2))
    x, y, w, h = map(float, face)
    factor = PRESETS[preset]
    # Expand downwards for shoulder/torso framing, with extra headroom.
    target_w = w * factor
    target_h = h * (factor * 1.25 if preset != "very-tight" else 1.25)
    center_y = y + h * (0.7 if preset == "very-tight" else 1.05)
    crop = _fit((x + w/2 - target_w/2, center_y - target_h/2, target_w, target_h),
                width, height)
    return {"crop": crop, "method": "face", "subjects": len(faces),
            "warning": "Multiple faces detected; crop targets the most prominent face." if len(faces) > 1 else None}
