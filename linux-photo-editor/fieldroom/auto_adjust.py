"""Conservative, local subject-aware tone and color suggestions.

Face/person detection chooses *where* to measure; it does not identify people or
infer their skin tone. Tone uses wide exposure deadbands, never a single target
face brightness. Color correction needs several spatially distinct, nearly
neutral patches at different brightnesses outside all detected faces and likely
skin. Ambiguous scenes retain the existing white balance.

The return value is a partial recipe, so omitted controls remain unchanged. No
image is modified, no network request is made, and detector/setup errors are not
quietly replaced with a different subject or a whole-image adjustment.
"""
from __future__ import annotations

import math
from numbers import Real
from pathlib import Path

import numpy as np

from . import imaging as im
from . import smartcrop as sc


def _validate(kind, subject_ids, crop):
    if kind not in ("tone", "color"):
        raise ValueError("Choose Auto Tone or Auto Color.")
    if subject_ids is not None:
        if not isinstance(subject_ids, list) or not subject_ids or not all(
                isinstance(value, str) and value for value in subject_ids):
            raise ValueError("Select at least one detected person.")
        if len(set(subject_ids)) != len(subject_ids):
            raise ValueError("Each selected person must appear only once.")
    crop = [0., 0., 1., 1.] if crop is None else crop
    if not isinstance(crop, (list, tuple)) or len(crop) != 4 or any(
            isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value)
            for value in crop):
        raise ValueError("The crop must contain four finite coordinates.")
    if not (0 <= crop[0] < crop[2] <= 1 and 0 <= crop[1] < crop[3] <= 1):
        raise ValueError("The crop must lie inside the photograph.")
    return tuple(float(value) for value in crop)


def _intersection(a, b):
    return max(0., min(a[2], b[2])-max(a[0], b[0])) * max(0., min(a[3], b[3])-max(a[1], b[1]))


def _choose(subjects, subject_ids, crop):
    if subject_ids is not None:
        known = {subject.id for subject in subjects}
        if any(value not in known for value in subject_ids):
            raise ValueError("A selected person is no longer available. Detect the people again.")
        selected = [subject for subject in subjects if subject.id in subject_ids]
        if any(_intersection(subject.detection.box, crop) <= 0 for subject in selected):
            raise ValueError("A selected person is outside the current crop. Adjust the crop or choose someone inside it.")
        return selected
    # Match Smart Crop's prominence rule while respecting the accepted crop.
    faces = [subject for subject in subjects if subject.face and _intersection(subject.face.box, crop) > 0]
    people = [subject for subject in subjects if subject.person and _intersection(subject.person.box, crop) > 0]
    eligible = faces or people
    if not eligible:
        return []
    return [max(eligible, key=lambda subject: _intersection(
        (subject.face if faces else subject.person).box, crop) *
        (subject.face if faces else subject.person).confidence)]


def _box_mask(shape, box, *, inset=0., ellipse=False):
    height, width = shape
    l, t, r, b = box
    dx, dy = (r-l)*inset, (b-t)*inset
    l, t, r, b = l+dx, t+dy, r-dx, b-dy
    yy, xx = np.ogrid[:height, :width]
    x, y = (xx+.5)/width, (yy+.5)/height
    if ellipse:
        return ((x-(l+r)/2)/max((r-l)/2, 1/width))**2 + (
            (y-(t+b)/2)/max((b-t)/2, 1/height))**2 <= 1
    return (x >= l) & (x < r) & (y >= t) & (y < b)


def _expanded(box, margin):
    l, t, r, b = box
    return (max(0., l-(r-l)*margin), max(0., t-(b-t)*margin),
            min(1., r+(r-l)*margin), min(1., b+(b-t)*margin))


def _regions(rgb, selected, frame):
    regions = []
    for subject in selected:
        face = _box_mask(rgb.shape[:2], subject.face.box, inset=.09, ellipse=True) & frame if subject.face else None
        if face is not None and np.count_nonzero(face) >= 32:
            regions.append((face, "face"))
        elif subject.person:
            body = _box_mask(rgb.shape[:2], subject.person.box, inset=.08) & frame
            if np.count_nonzero(body) >= 32:
                regions.append((body, "person"))
                continue
            raise ValueError("A selected person is too small in the current crop for a reliable automatic adjustment.")
        else:
            raise ValueError("A selected face is too small in the current crop for a reliable automatic adjustment.")
    return regions


def _tone(rgb, frame, regions):
    luma = rgb[:, :, 0]*.2126 + rgb[:, :, 1]*.7152 + rgb[:, :, 2]*.0722
    proposals, shadow_lifts = [], []
    for mask, kind in regions or [(frame, "fallback")]:
        q20, q80 = np.percentile(luma[mask], [20, 80])
        # A broad neutral zone preserves reflectance differences. Modest changes
        # start only when most of a region is very dark or close to clipping.
        dark_limit, max_lift = (.24, .70) if kind == "face" else (.16, .35)
        dark = float(np.clip((dark_limit-q80)/dark_limit, 0, 1))
        bright = float(np.clip((q80-.88)/.12, 0, 1))
        proposals.append(max_lift*dark - (.45 if kind == "face" else .30)*bright)
        shadow_lifts.append(min(26., max(0., (.18-float(q20))*105)))
    # Equal influence per selected person avoids a large foreground face
    # completely overriding another selected person.
    exposure = float(np.mean(proposals))
    shadows = float(np.mean(shadow_lifts))
    frame_values = rgb[frame]
    high = float(np.percentile(np.max(frame_values, axis=1), 99))
    linear_high = high/12.92 if high <= .04045 else ((high+.055)/1.055)**2.4
    safe_lift = max(0., math.log2(.985/max(linear_high, 1e-5)))
    limited = exposure > safe_lift + .015
    if limited:
        shadows = min(28., shadows + (exposure-safe_lift)*15)
        exposure = safe_lift
    highlights = -round(min(30., max(0., (high-.82)/.18*30)))
    recipe = {"exposure": round(exposure, 2), "shadows": round(shadows), "highlights": highlights,
              "contrast": 0, "whites": 0, "blacks": 0}
    warning = "Bright areas limited the exposure lift; a restrained shadow adjustment was used instead." if limited else None
    return recipe, warning


def _neutral_color(rgb, frame, subjects, selected):
    region = np.zeros(rgb.shape[:2], dtype=bool)
    if selected:
        for subject in selected:
            region |= _box_mask(rgb.shape[:2], _expanded(subject.detection.box, .12 if subject.person else .75))
        region &= frame
    else:
        region = frame.copy()
    # Exclude every face, including unselected people near the selected subject.
    for subject in subjects:
        if subject.face:
            region &= ~_box_mask(rgb.shape[:2], _expanded(subject.face.box, .30))
    r, g, b = (rgb[:, :, channel] for channel in range(3))
    likely_skin = (r-g > .035) & (r-b > .09) & (r > g*1.04) & (g > b*.9)
    region &= ~likely_skin
    height, width = region.shape
    edge = max(8, min(height, width)//24)
    patches = []
    for y in range(0, height-edge+1, edge):
        for x in range(0, width-edge+1, edge):
            mask = region[y:y+edge, x:x+edge]
            if np.count_nonzero(mask) < edge*edge*.90:
                continue
            pixels = rgb[y:y+edge, x:x+edge][mask]
            maximum, minimum = pixels.max(axis=1), pixels.min(axis=1)
            chroma = (maximum-minimum)/np.maximum(maximum, .01)
            light = pixels @ np.array([.2126, .7152, .0722])
            # Near-neutrals only. Colored clothing, dark objects and clipped
            # white areas are not white-balance references.
            if (np.percentile(chroma, 90) > .10 or np.median(light) < .25 or np.median(light) > .85
                    or np.percentile(maximum, 99) > .94 or np.ptp(np.percentile(light, [10, 90])) > .12):
                continue
            linear = np.where(pixels <= .04045, pixels/12.92, ((pixels+.055)/1.055)**2.4)
            med = np.median(linear, axis=0)
            ratios = np.log(np.maximum(med[[0, 2]], .001)/max(float(med[1]), .001))
            patches.append((x+edge/2, y+edge/2, float(np.median(light)), *ratios))
    if len(patches) < 4:
        return {}, False
    values = np.asarray(patches)
    # A single flat garment/wall is ambiguous. Demand separated patches with
    # different brightness but a consistent, very small channel imbalance.
    if (max(np.ptp(values[:, 0]), np.ptp(values[:, 1])) < edge*3
            or np.ptp(np.percentile(values[:, 2], [20, 80])) < .08):
        return {}, False
    ratios = np.median(values[:, 3:5], axis=0)
    disagreement = np.percentile(np.abs(values[:, 3:5]-ratios), 80, axis=0)
    if np.any(disagreement > .025):
        return {}, False
    red, blue = np.exp(ratios)
    full_warmth = (blue-red)/(.24*(blue+red))*100
    full_warmth = float(np.clip(full_warmth, -20, 20))
    balanced = (red*(1+.24*full_warmth/100) + blue*(1-.24*full_warmth/100))/2
    full_tint = float(np.clip((1-balanced)/.20*100, -20, 20))
    # Half-strength, tightly bounded corrections deliberately retain some cast;
    # these image-only neutral candidates are evidence, not a calibrated card.
    return {"warmth": round(full_warmth*.5), "tint": round(full_tint*.5)}, True


def suggest(path, kind="tone", subject_ids=None, crop=None):
    """Return a partial suggested recipe using the selected/main visible people.

    The accepted crop limits measurement, but subject IDs remain those from the
    complete oriented photograph. Missing models/invalid explicit selections
    raise an error. Only successful detection with no person in the crop uses a
    clearly reported, restrained whole-image fallback.
    """
    crop = _validate(kind, subject_ids, crop)
    path = Path(path).expanduser().resolve(strict=True)
    before = path.stat()
    fingerprint = (before.st_size, before.st_mtime_ns)
    analysis = sc.analyze(path)
    source = im.decode(path, edge=960, thumbnail=False)
    after = path.stat()
    if fingerprint != (after.st_size, after.st_mtime_ns) or tuple(source.size) != tuple(analysis.size):
        raise ValueError("The original changed during analysis. Reopen the folder and try again.")
    rgb = np.asarray(im.render(source, im.recipe()).convert("RGB"), dtype=np.float32)/255
    subjects = sc._subjects(analysis.detections)
    selected = _choose(subjects, subject_ids, crop)
    frame = _box_mask(rgb.shape[:2], crop)
    if np.count_nonzero(frame) < 32:
        raise ValueError("The crop is too small for a reliable automatic adjustment.")
    regions = _regions(rgb, selected, frame)
    face_count = sum(kind == "face" for _, kind in regions)
    method = "subject-face" if face_count else "subject-person" if selected else "whole-image-fallback"
    scope = (f"{len(selected)} selected {'person' if len(selected) == 1 else 'people'}"
             if subject_ids is not None else "the main detected person") if selected else "the current image"
    warning = None
    if kind == "tone":
        values, warning = _tone(rgb, frame, regions)
        message = f"Auto Tone considered {scope}, prioritizing visible faces." if face_count else f"Auto Tone considered {scope}."
        if selected and face_count < len(selected):
            body_note = "Some selected faces were not visible; their detected body regions were measured conservatively."
            warning = " ".join(filter(None, (warning, body_note)))
    else:
        values, reliable = _neutral_color(rgb, frame, subjects, selected)
        if reliable:
            message = f"Auto Color used consistent near-neutral areas around {scope} for a small white-balance correction."
        else:
            message = f"Auto Color checked {scope}. White balance is unchanged because no reliable neutral reference was found."
            warning = "Faces, likely skin and strongly colored clothing are excluded from neutral estimation."
    if not selected:
        fallback = "No person was detected reliably in the current crop. A restrained whole-image fallback was used."
        message = fallback + " " + message
        warning = " ".join(filter(None, (warning, fallback)))
    choices = [subject.public() for subject in subjects]
    if len(subjects) > 1:
        choices.append(sc._Group(tuple(subjects)).public())
    return {"recipe": values, "method": method, "message": message, "warning": warning,
            "selected_subjects": [subject.id for subject in selected], "subjects": choices}
