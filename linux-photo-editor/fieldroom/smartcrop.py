"""Subject-aware crop suggestions from local, confidence-gated detections.

The detector proposes a frame only; the user's recipe is changed only by Apply
in the editor. An uncertain detection returns crop=None, never a center crop.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
from pathlib import Path
import threading

import numpy as np

from . import imaging as im
from .detectors import Detection, detect_rgb
from .models import ModelUnavailable

PRESETS = ("very-tight", "tight", "normal", "loose", "full-body")
ASPECTS = ("Free", "Original", "1:1", "3:2", "2:3", "4:5", "5:4", "16:9")


@dataclass(frozen=True)
class Analysis:
    size: tuple[int, int]  # original oriented image dimensions, not preview size
    detections: tuple[Detection, ...]


@dataclass(frozen=True)
class _Subject:
    id: str
    label: str
    person: Detection | None
    face: Detection | None

    @property
    def detection(self):
        return self.person or self.face

    def public(self):
        d = self.detection
        return {"id": self.id, "label": self.label, "box": list(d.box),
                "confidence": round(d.confidence, 3), "kind": d.kind}


@dataclass(frozen=True)
class _Group:
    members: tuple[_Subject, ...]
    id: str = "all"

    def public(self):
        return {"id": self.id, "label": f"All people ({len(self.members)})",
                "box": _union(s.detection.box for s in self.members),
                "confidence": round(min(s.detection.confidence for s in self.members), 3),
                "kind": "group", "member_ids": [s.id for s in self.members]}


_ANALYSIS_LOCK = threading.Lock()
_CACHE = OrderedDict()


def analyze(path):
    """Reuse one oriented decode/inference across presets and subject choices.

    A bounded metadata-only cache and one lock keep repeated requests from
    decoding large RAWs or sharing mutable DNN objects concurrently.
    """
    path = Path(path).expanduser().resolve(strict=True)
    with _ANALYSIS_LOCK:
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
        source = im.decode(path, edge=1280, thumbnail=False)
        preview = im.render(source, im.recipe())
        detections = detect_rgb(np.asarray(preview.convert("RGB")))
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != key[1:]:
            raise ValueError("The original changed during detection. Reopen the folder and try again.")
        result = Analysis(tuple(source.size), detections)
        _CACHE[key] = result
        while len(_CACHE) > 6:
            _CACHE.popitem(last=False)
        return result


def _area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def _valid(d):
    return (d.kind in ("face", "person") and len(d.box) == 4
            and all(math.isfinite(v) for v in (*d.box, d.confidence))
            and 0 <= d.box[0] < d.box[2] <= 1 and 0 <= d.box[1] < d.box[3] <= 1
            and 0 <= d.confidence <= 1)


def _matches(face, person):
    """Require a face within the upper person; a mark on an arm is no match."""
    fl, ft, fr, fb = face.box
    pl, pt, pr, pb = person.box
    overlap = max(0, min(fr, pr) - max(fl, pl)) * max(0, min(fb, pb) - max(ft, pt))
    return (overlap >= .75 * _area(face.box)
            and pl <= (fl + fr) / 2 <= pr
            and pt <= (ft + fb) / 2 <= pt + .62 * (pb - pt)
            and pb - pt >= (fb - ft) * 1.15)


def _subjects(detections):
    faces = [d for d in detections if _valid(d) and d.kind == "face" and d.confidence >= .85]
    people = [d for d in detections if _valid(d) and d.kind == "person" and d.confidence >= .55]
    people = [d for d in people if d.confidence >= .70 or any(_matches(f, d) for f in faces)]
    people.sort(key=lambda d: (d.box[0], d.box[1], -d.confidence))
    # Assign each face to its best spatially plausible person, not the closest
    # photo center. The smallest enclosing person avoids nested duplicate boxes.
    matched = {i: [] for i in range(len(people))}
    unassigned = []
    for face in faces:
        choices = [i for i, p in enumerate(people) if _matches(face, p)]
        if choices:
            index = min(choices, key=lambda i: _area(people[i].box))
            matched[index].append(face)
        elif face.confidence >= .90:
            unassigned.append(face)
    def position(d):
        x = (d.box[0] + d.box[2]) / 2
        return "left" if x < .35 else "right" if x > .65 else "middle"
    subjects = []
    for i, person in enumerate(people):
        face = max(matched[i], key=lambda f: _area(f.box) * f.confidence, default=None)
        subjects.append(_Subject(f"person-{i+1}", f"Person {i+1} ({position(person)})", person, face))
        # A parent holding a child can produce one body box and two faces. Keep
        # the additional confident face selectable without inventing another body.
        unassigned.extend(f for f in matched[i] if f is not face and f.confidence >= .90)
    for i, face in enumerate(sorted(unassigned, key=lambda d: (d.box[0], d.box[1]))):
        subjects.append(_Subject(f"face-{i+1}", f"Face {i+1} ({position(face)})", None, face))
    return subjects


def _response(status, subjects=(), selected=None, crop=None, method="none", warning=None, aspect="Free"):
    choices = [s.public() for s in subjects]
    if len(subjects) > 1:
        choices.append(_Group(tuple(subjects)).public())
    selected_subjects = ([s.id for s in selected.members] if isinstance(selected, _Group)
                         else [selected.id] if selected else [])
    return {"status": status, "crop": crop, "method": method,
            "subjects": choices,
            "selected_subject": selected.id if selected else None,
            "selected_subjects": selected_subjects,
            "warning": warning, "aspect": aspect}


def _union(boxes):
    boxes = tuple(boxes)
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _clamp(box):
    return [max(0., box[0]), max(0., box[1]), min(1., box[2]), min(1., box[3])]


def _portrait(subject, preset):
    l, t, r, b = subject.face.box
    w, h = r-l, b-t
    # Separate upper and lower margins preserve hair/hat and then add shoulders.
    # These ordered margins make every wider preset include the tighter one.
    side, above, below = {
        "very-tight": (.50, .60, .40),
        "tight": (.75, .85, .90),
        "normal": (1.10, .95, 1.80),
        "loose": (1.90, 1.20, 3.40),
    }[preset]
    top = t - above * h
    if subject.person:
        pl, pt, pr, pb = subject.person.box
        # The person detector often includes a hat that the face box excludes.
        # Cap extra headroom: raised hands must not determine portrait framing.
        top = min(top, max(pt - .025 * (pb-pt), t - 1.15 * h))
    return _clamp((l-side*w, top, r+side*w, b+below*h))


def _full_body(subject):
    l, t, r, b = subject.person.box
    w, h = r-l, b-t
    return _clamp((l-.08*w, t-.06*h, r+.08*w, b+.08*h))


def _aspect(box, aspect, size):
    if aspect == "Free":
        return box, aspect, None
    width, height = size
    if aspect == "Original":
        ratio = 1.0  # normalized width/height; physical ratio is width/height
    else:
        x, y = map(float, aspect.split(":"))
        ratio = x / y * height / width
    l, t, r, b = box
    w, h = r-l, b-t
    if w / h < ratio:
        w = h * ratio
    else:
        h = w / ratio
    if w > 1 + 1e-9 or h > 1 + 1e-9:
        return box, "Free", "The chosen aspect ratio would cut into the framing. Free aspect keeps the subject intact."
    w, h = min(1., w), min(1., h)
    left = max(0., min(1-w, (l+r-w)/2))
    top = max(0., min(1-h, (t+b-h)/2))
    return [left, top, left+w, top+h], aspect, None


def _validate(preset, aspect):
    if preset not in PRESETS:
        raise ValueError("Unknown Smart Crop preset.")
    if aspect not in ASPECTS:
        raise ValueError("Unknown Smart Crop aspect ratio.")


def _ready(analysis, subjects, selected, box, method, warnings, aspect):
    box, fitted_aspect, warning = _aspect(box, aspect, analysis.size)
    if warning:
        warnings.append(warning.replace("the subject intact", "everyone in the frame")
                        if isinstance(selected, _Group) else warning)
    width = (box[2]-box[0]) * analysis.size[0]
    height = (box[3]-box[1]) * analysis.size[1]
    if min(width, height) < 512 or width*height < 1_000_000:
        warnings.append("This crop has limited resolution. Check the cropped size before exporting; Fieldroom does not upscale it.")
    # Round outward so serialization cannot shave off a boundary at the edge.
    crop = [math.floor(box[0]*1e6)/1e6, math.floor(box[1]*1e6)/1e6,
            math.ceil(box[2]*1e6)/1e6, math.ceil(box[3]*1e6)/1e6]
    return _response("ready", subjects, selected, crop, method, " ".join(warnings) or None, fitted_aspect)


def _group_crop(analysis, subjects, members, preset, aspect):
    selected = _Group(tuple(members), "all" if len(members) == len(subjects) else "group")
    if preset == "full-body" and any(s.person is None for s in members):
        return _response("person_not_detected", subjects, selected,
                         warning="A whole-person outline was not detected for everyone selected. Your crop is unchanged; try a portrait preset or adjust the group crop manually.", aspect=aspect)
    frames, bodies, warnings = [], [], []
    for subject in members:
        if preset == "full-body" or subject.face is None:
            # A turned-away member must not disappear from a group portrait.
            # Include their reliable body outline; never guess their head position.
            frames.append(_full_body(subject))
            bodies.append(subject.person)
        else:
            frames.append(_portrait(subject, preset))
    if preset != "full-body" and bodies:
        warnings.append("Some faces are not clear enough for a portrait crop. Their full visible bodies are included to keep everyone in the frame.")
    if any(min(d.box[0], d.box[1], 1-d.box[2], 1-d.box[3]) < .02 for d in bodies):
        warnings.append("Someone touches the photo edge. Review heads, hands and feet; parts outside the original cannot be restored.")
    return _ready(analysis, subjects, selected, _union(frames), "group", warnings, aspect)


def suggest_from_analysis(analysis, preset="tight", subject_id=None, aspect="Free", *, subject_ids=None):
    """Pure framing policy, separated from model inference for regression tests."""
    _validate(preset, aspect)
    if len(analysis.size) != 2 or min(analysis.size) <= 0:
        raise ValueError("Invalid photograph dimensions.")
    subjects = _subjects(analysis.detections)
    if subject_ids is not None:
        if subject_id is not None:
            raise ValueError("Choose a single subject or a list of people, not both.")
        if not isinstance(subject_ids, list) or not subject_ids or not all(isinstance(i, str) for i in subject_ids):
            raise ValueError("Select at least one detected person.")
        if len(set(subject_ids)) != len(subject_ids):
            raise ValueError("Each selected person must appear only once.")
        known = {s.id for s in subjects}
        if any(i not in known for i in subject_ids):
            raise ValueError("A selected person is no longer available. Preview Smart Crop again.")
        members = [s for s in subjects if s.id in subject_ids]
        if len(members) > 1:
            return _group_crop(analysis, subjects, members, preset, aspect)
        subject_id = members[0].id
    if subject_id == "all" and len(subjects) > 1:
        return _group_crop(analysis, subjects, subjects, preset, aspect)
    if subject_id is not None and not any(s.id == subject_id for s in subjects):
        raise ValueError("That subject is no longer available. Preview Smart Crop again.")
    if not subjects:
        return _response("no_subject", warning="Subject not detected reliably. Your crop is unchanged; adjust it manually.", aspect=aspect)
    eligible = [s for s in subjects if (s.person if preset == "full-body" else s.face)]
    if subject_id is not None:
        selected = next(s for s in subjects if s.id == subject_id)
    else:
        def prominence(s):
            d = s.person if preset == "full-body" else s.face
            return _area(d.box) * d.confidence if d else 0
        selected = max(eligible or subjects, key=prominence)
    if preset == "full-body" and not selected.person:
        return _response("person_not_detected", subjects, selected,
                         warning="A face was found, but a reliable whole-person outline was not. Your crop is unchanged; use a portrait preset or adjust manually.", aspect=aspect)
    if preset != "full-body" and not selected.face:
        return _response("face_not_detected", subjects, selected,
                         warning="A person was found, but their face is not clear enough for a tight portrait. Your crop is unchanged; try Full Body or adjust manually.", aspect=aspect)
    warnings = []
    if preset == "full-body":
        box = _full_body(selected)
        method = "person"
        l, t, r, b = selected.person.box
        if min(l, t, 1-r, 1-b) < .02:
            warnings.append("The person touches the photo edge. Review the head, hands and feet; parts outside the original cannot be restored.")
    else:
        box = _portrait(selected, preset)
        method = "face"
    return _ready(analysis, subjects, selected, box, method, warnings, aspect)


def suggest(path, preset="tight", subject_id=None, aspect="Free", *, subject_ids=None):
    _validate(preset, aspect)
    try:
        analysis = analyze(path)
    except ModelUnavailable as exc:
        return _response("model_unavailable", warning=str(exc), aspect=aspect)
    return suggest_from_analysis(analysis, preset, subject_id, aspect, subject_ids=subject_ids)
