"""Reversible, CPU-only neural denoising and detail restoration.

DnCNN predicts a clean RGB image. The small pretrained ESPCN network predicts
3x luminance; that prediction is reduced back to the original size before its
luminance residual is blended in. This is a modest learned detail treatment,
not a promise to recover missed focus or motion-blurred information.

Both models run on bounded overlapping input tiles. Halos are discarded, not
joined into the output, so internal model padding cannot create tile seams.
The caller caches a complete enhanced source before preview resizing/cropping.
There is no network access anywhere in this module.
"""
from __future__ import annotations

import math
import threading

import numpy as np
from PIL import Image

from .enhance_models import EnhancementUnavailable, locate_models


class EnhancementCancelled(RuntimeError):
    """A cancelled job never returns a partially processed photograph."""


_lock = threading.RLock()
_sessions: dict[tuple, object] = {}
DENOISE_CORE = 512
DENOISE_HALO = 24  # DnCNN has twenty 3x3 convolutions (radius 20).
DETAIL_INPUT = 224  # The verified ONNX graph has a fixed spatial input.
DETAIL_HALO = 8
DETAIL_CORE = DETAIL_INPUT - 2 * DETAIL_HALO


def _runtime():
    try:
        import onnxruntime as ort
    except (ImportError, OSError) as exc:
        raise EnhancementUnavailable("AI detail needs ONNX Runtime. Run the updated Fieldroom installer.") from exc
    return ort


def _session(role, directory=None):
    path = locate_models(directory, [role])[role]
    stat = path.stat()
    key = (role, str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if key not in _sessions:
        ort = _runtime()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        try:
            session = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
        except Exception as exc:
            raise EnhancementUnavailable(f"The offline {role} model could not load. Run the updated installer.") from exc
        expected = 3 if role == "denoise" else 1
        inputs = session.get_inputs()
        if len(inputs) != 1 or inputs[0].type != "tensor(float)" or inputs[0].shape[1] != expected:
            raise EnhancementUnavailable(f"The offline {role} model has an unexpected input format.")
        if len(_sessions) > 3:
            _sessions.clear()
        _sessions[key] = session
    return _sessions[key]


def status(directory=None) -> dict:
    result = {"available": False, "denoise": False, "sharpen": False, "backend": "CPU", "message": ""}
    try:
        _runtime()
        for role in ("denoise", "sharpen"):
            locate_models(directory, [role])
            result[role] = True
        result["available"] = True
        result["message"] = "Offline AI denoise and detail are ready."
    except EnhancementUnavailable as exc:
        result["message"] = str(exc)
    return result


def _predict(session, blob, expected_shape):
    try:
        value = session.run(None, {session.get_inputs()[0].name: np.ascontiguousarray(blob, dtype=np.float32)})[0]
    except Exception as exc:
        raise EnhancementUnavailable("The offline AI model could not process this image. Your existing edit is unchanged.") from exc
    if value.shape != expected_shape or not np.isfinite(value).all():
        raise EnhancementUnavailable("The offline AI model returned an invalid image. Your existing edit is unchanged.")
    return value


def check_runtime(directory=None):
    with _lock:
        denoise = _predict(_session("denoise", directory), np.full((1, 3, 32, 32), .5, np.float32), (1, 3, 32, 32))
        detail = _predict(_session("sharpen", directory), np.full((1, 1, DETAIL_INPUT, DETAIL_INPUT), .5, np.float32),
                          (1, 1, DETAIL_INPUT * 3, DETAIL_INPUT * 3))
        if not (0 < float(denoise.mean()) < 1 and 0 < float(detail.mean()) < 1):
            raise EnhancementUnavailable("AI detail inference check failed.")


def _strength(value):
    if isinstance(value, (bool, str)):
        raise ValueError("AI detail strength must be a number between 0 and 100.")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("AI detail strength must be a number between 0 and 100.") from exc
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError("AI detail strength must be a number between 0 and 100.")
    return value / 100


def _patch(rgb, x, y, core_width, core_height, halo):
    """Build only this tile, reflecting at the actual photograph boundary."""
    h, w = rgb.shape[:2]
    left, top = max(0, x - halo), max(0, y - halo)
    right, bottom = min(w, x + core_width + halo), min(h, y + core_height + halo)
    tile = rgb[top:bottom, left:right]
    pads = ((max(0, halo - y), max(0, y + core_height + halo - h)),
            (max(0, halo - x), max(0, x + core_width + halo - w)), (0, 0))
    if any(amount for axis in pads for amount in axis):
        tile = np.pad(tile, pads, mode="reflect" if min(tile.shape[:2]) > 1 else "edge")
    return np.ascontiguousarray(tile, dtype=np.float32)


def apply_rgb(rgb, denoise=0, sharpen=0, *, directory=None, progress=None, cancelled=None):
    """Return same-size float RGB in sRGB 0..1 without modifying ``rgb``.

    ``progress(fraction, message)`` receives fractions in 0..1. ``cancelled()``
    is checked before and after each tile and raises EnhancementCancelled.
    All model work is serialized to bound CPU and inference-buffer memory.
    """
    denoise, sharpen = _strength(denoise), _strength(sharpen)
    source = np.asarray(rgb)
    if source.ndim != 3 or source.shape[2] != 3 or min(source.shape[:2]) < 1:
        raise ValueError("AI detail needs a nonempty RGB image.")
    if source.dtype.kind != "f" or not np.isfinite(source).all() or source.min() < 0 or source.max() > 1:
        raise ValueError("AI detail needs finite floating-point sRGB values between 0 and 1.")
    h, w = source.shape[:2]
    cancelled = cancelled or (lambda: False)
    progress = progress or (lambda fraction, message: None)

    def check_cancel():
        if cancelled():
            raise EnhancementCancelled("AI detail preview cancelled. Your existing edit is unchanged.")

    check_cancel()
    if not denoise and not sharpen:
        progress(1.0, "AI detail is off.")
        return source.astype(np.float32, copy=True)
    total = (math.ceil(w / DENOISE_CORE) * math.ceil(h / DENOISE_CORE) if denoise else 0)
    total += math.ceil(w / DETAIL_CORE) * math.ceil(h / DETAIL_CORE) if sharpen else 0
    done = 0
    progress(0.0, "Preparing offline AI detail…")
    # Check the complete requested installation before processing any pixels.
    with _lock:
        check_cancel()
        networks = {role: _session(role, directory) for role, amount in (("denoise", denoise), ("sharpen", sharpen)) if amount}
        current = source
        if denoise:
            output = np.empty((h, w, 3), dtype=np.float32)
            for y in range(0, h, DENOISE_CORE):
                for x in range(0, w, DENOISE_CORE):
                    check_cancel()
                    cw, ch = min(DENOISE_CORE, w - x), min(DENOISE_CORE, h - y)
                    tile = _patch(current, x, y, cw, ch, DENOISE_HALO)
                    blob = tile.transpose(2, 0, 1)[None]
                    clean = _predict(networks["denoise"], blob, blob.shape)[0].transpose(1, 2, 0)
                    check_cancel()
                    clean = clean[DENOISE_HALO:DENOISE_HALO + ch, DENOISE_HALO:DENOISE_HALO + cw]
                    original = current[y:y + ch, x:x + cw]
                    output[y:y + ch, x:x + cw] = np.clip(original + (clean - original) * denoise, 0, 1)
                    done += 1
                    progress(done / total, "Reducing noise on this computer…")
            current = output
        if sharpen:
            output = np.empty((h, w, 3), dtype=np.float32)
            for y in range(0, h, DETAIL_CORE):
                for x in range(0, w, DETAIL_CORE):
                    check_cancel()
                    cw, ch = min(DETAIL_CORE, w - x), min(DETAIL_CORE, h - y)
                    tile = _patch(current, x, y, DETAIL_CORE, DETAIL_CORE, DETAIL_HALO)
                    luma = tile[:, :, 0] * .299 + tile[:, :, 1] * .587 + tile[:, :, 2] * .114
                    predicted = _predict(networks["sharpen"], luma[None, None],
                                         (1, 1, DETAIL_INPUT * 3, DETAIL_INPUT * 3))[0, 0]
                    check_cancel()
                    # Float-mode resize retains fractional samples and avoids an
                    # intermediate 8-bit rounding before the RAW pipeline resumes.
                    restored = np.asarray(Image.fromarray(predicted).resize((DETAIL_INPUT, DETAIL_INPUT), Image.Resampling.LANCZOS))
                    difference = (restored - luma)[DETAIL_HALO:DETAIL_HALO + ch, DETAIL_HALO:DETAIL_HALO + cw]
                    original = current[y:y + ch, x:x + cw]
                    output[y:y + ch, x:x + cw] = np.clip(original + difference[:, :, None] * sharpen, 0, 1)
                    done += 1
                    progress(done / total, "Restoring detail on this computer…")
            current = output
        check_cancel()
        progress(1.0, "Offline AI detail preview ready.")
        return current


def apply(image: Image.Image, denoise=0, sharpen=0, **kwargs) -> Image.Image:
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255
    result = apply_rgb(rgb, denoise, sharpen, **kwargs)
    return Image.fromarray(np.clip(result * 255 + .5, 0, 255).astype(np.uint8))
