"""Photo decoding and the shared preview/export rendering pipeline."""
from __future__ import annotations

import io
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageFilter, ImageOps

try:
    import rawpy
except (ImportError, OSError):
    rawpy = None

RAWS = set(".nef .nrw .cr2 .cr3 .crw .arw .sr2 .srf .dng .raf .orf .rw2 .pef .3fr .iiq .mos .kdc .dcr .mrw .erf .srw".split())
FORMATS = RAWS | set(".jpg .jpeg .png .tif .tiff .webp .bmp".split())
LIMITS = {
    "exposure": (-4, 4), "contrast": (-100, 100), "highlights": (-100, 100),
    "shadows": (-100, 100), "whites": (-100, 100), "blacks": (-100, 100),
    "warmth": (-100, 100), "saturation": (-100, 100), "luminance": (0, 100),
    "color": (0, 100), "sharpen": (0, 100), "radius": (0.5, 3),
}
DEFAULTS = {name: (1 if name == "radius" else 0) for name in LIMITS}
FULL = (0.0, 0.0, 1.0, 1.0)
SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
LEVELS = np.arange(256, dtype=np.float32) / 255
TO_LINEAR = np.where(LEVELS <= 0.04045, LEVELS / 12.92, ((LEVELS + 0.055) / 1.055) ** 2.4)


def recipe(values=None):
    values = values or {}
    result = dict(DEFAULTS)
    for name, (low, high) in LIMITS.items():
        value = float(values.get(name, result[name]))
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"Invalid {name} value.")
        result[name] = value
    crop = tuple(map(float, values.get("crop", FULL)))
    if len(crop) != 4 or not (0 <= crop[0] < crop[2] <= 1 and 0 <= crop[1] < crop[3] <= 1):
        raise ValueError("The crop must lie inside the photograph.")
    result["crop"] = list(crop)
    return result


@dataclass
class Source:
    pixels: object  # PIL RGB image (sRGB) or a linear uint16 RGB array from LibRaw
    size: tuple
    metadata: dict
    exif: bytes = b""


def read_metadata(image):
    tags = image.getexif()
    try:
        detailed = tags.get_ifd(0x8769)
    except (OSError, ValueError, TypeError, SyntaxError):
        detailed = {}
    def field(tag):
        return detailed.get(tag, tags.get(tag))
    def number(tag):
        try:
            return float(field(tag))
        except (ValueError, TypeError, ZeroDivisionError):
            return None
    shutter, aperture, focal = number(33434), number(33437), number(37386)
    data = {k:v for k,v in {
        "camera": str(field(272) or "").strip("\x00 "),
        "iso": f"ISO {field(34855)}" if field(34855) else "",
        "aperture": f"ƒ/{aperture:g}" if aperture else "",
        "shutter": (f"1/{round(1/shutter)} s" if shutter < 0.5 else f"{shutter:g} s") if shutter else "",
        "lens": f"{focal:g} mm" if focal else "",
    }.items() if v}
    # A new JPEG has new dimensions, orientation and pixel offsets. Carry only
    # descriptive/capture fields. A RAW maker note can contain TIFF-relative offsets.
    cleaned = Image.Exif()
    for tag in (270, 271, 272, 306, 315, 33432):
        if tags.get(tag) is not None:
            cleaned[tag] = tags[tag]
    capture = {tag: field(tag) for tag in (33434, 33437, 34855, 36867, 36868, 37377,
               37378, 37380, 37385, 37386, 41989, 42035, 42036) if field(tag) is not None}
    if capture:
        cleaned[0x8769] = capture
    return data, cleaned.tobytes() if len(cleaned) else b""


def raster(path, edge):
    with Image.open(path) as opened:
        meta, exif = read_metadata(opened)
        orientation = opened.getexif().get(274, 1)
        size = opened.size[::-1] if orientation in (5, 6, 7, 8) else opened.size
        if edge:
            opened.draft("RGB", (edge, edge))
        image = ImageOps.exif_transpose(opened)
        if image.mode == "RGBA" or "transparency" in image.info:
            alpha = image.convert("RGBA")
            background = Image.new("RGB", alpha.size, "white")
            background.paste(alpha, mask=alpha.getchannel("A"))
            image = background
        profile = opened.info.get("icc_profile")
        if profile and image.mode in ("RGB", "CMYK", "LAB", "L"):
            try:
                image = ImageCms.profileToProfile(image, io.BytesIO(profile), io.BytesIO(SRGB), outputMode="RGB")
            except (ImageCms.PyCMSError, OSError, ValueError):
                image = image.convert("RGB")
        else:
            image = image.convert("RGB")
        if edge:
            image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        return Source(image, size, meta, exif)


def decode(path: Path, edge=0, thumbnail=False):
    if path.suffix.lower() not in RAWS:
        return raster(path, edge)
    if rawpy is None:
        raise ValueError("RAW decoding is unavailable in this installation. Install the RAW dependencies listed in the README.")
    try:
        with rawpy.imread(str(path)) as raw:
            dims = raw.sizes
            size = (dims.iwidth, dims.iheight)
            if dims.flip in (5, 6):
                size = size[::-1]
            if thumbnail:
                try:
                    thumb = raw.extract_thumb()
                    if thumb.format == rawpy.ThumbFormat.JPEG:
                        with Image.open(io.BytesIO(thumb.data)) as embedded:
                            image = ImageOps.exif_transpose(embedded).convert("RGB")
                    else:
                        image = Image.fromarray(thumb.data).convert("RGB")
                    image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                    return Source(image, size, {})
                except (rawpy.LibRawError, OSError, ValueError):
                    pass
            pixels = raw.postprocess(use_camera_wb=True, no_auto_bright=True, half_size=bool(edge),
                                     output_bps=16, gamma=(1, 1), output_color=rawpy.ColorSpace.sRGB)
    except (rawpy.LibRawError, OSError) as exc:
        raise ValueError(f"Cannot develop {path.name}. The file or camera compression may be unsupported by this LibRaw build. {exc}") from exc
    if edge and max(pixels.shape[:2]) > edge:
        h, w = pixels.shape[:2]
        factor = edge / max(w, h)
        target = (max(1, round(w * factor)), max(1, round(h * factor)))
        pixels = np.stack([np.asarray(Image.fromarray(pixels[:, :, c]).resize(target, Image.Resampling.BILINEAR)) for c in range(3)], axis=2)
    if not edge:
        size = (pixels.shape[1], pixels.shape[0])
    meta, exif = {}, b""
    try:
        with Image.open(path) as tags:
            meta, exif = read_metadata(tags)
    except (OSError, ValueError, TypeError, SyntaxError):
        pass
    return Source(pixels, size, meta, exif)


@lru_cache(maxsize=3)
def preview_source(path, fingerprint, edge):
    return decode(path, edge)


def pixel_box(size, crop):
    w, h = size
    x0 = min(w - 1, round(w * crop[0]))
    y0 = min(h - 1, round(h * crop[1]))
    return x0, y0, max(x0 + 1, round(w * crop[2])), max(y0 + 1, round(h * crop[3]))


def apply_detail(image, r):
    if not any(r[k] for k in ("luminance", "color", "sharpen")):
        return image
    y, cb, cr = image.convert("YCbCr").split()
    if r["luminance"]:
        y = Image.blend(y, y.filter(ImageFilter.MedianFilter(3)), r["luminance"] / 100)
    if r["color"]:
        strength = r["color"] / 100
        blur = ImageFilter.GaussianBlur(1 + 2 * strength)
        cb = Image.blend(cb, cb.filter(blur), strength)
        cr = Image.blend(cr, cr.filter(blur), strength)
    if r["sharpen"]:
        y = y.filter(ImageFilter.UnsharpMask(radius=r["radius"], percent=round(2.3*r["sharpen"]), threshold=2))
    return Image.merge("YCbCr", (y, cb, cr)).convert("RGB")


def render(source, settings, *, box=None):
    r = recipe(settings)
    raster_input = isinstance(source.pixels, Image.Image)
    size = source.pixels.size if raster_input else (source.pixels.shape[1], source.pixels.shape[0])
    if box is None:
        box = pixel_box(size, r["crop"])
    tone_controls = ("exposure", "contrast", "highlights", "shadows", "whites", "blacks", "warmth", "saturation")
    if raster_input and not any(r[k] for k in tone_controls):
        return apply_detail(source.pixels.crop(box), r)
    x0, y0, x1, y1 = box
    result = np.empty((y1-y0, x1-x0, 3), dtype=np.uint8)
    gain = 2 ** r["exposure"]
    warmth = r["warmth"] / 100
    gains = np.array([1 + .24 * warmth, 1, 1 - .24 * warmth], dtype=np.float32) * gain
    # Bound floating-point working memory for 45+ megapixel RAW files.
    for top in range(y0, y1, 160):
        bottom = min(top + 160, y1)
        if raster_input:
            linear = TO_LINEAR[np.asarray(source.pixels.crop((x0, top, x1, bottom)))]
        else:
            linear = source.pixels[top:bottom, x0:x1].astype(np.float32) / 65535
        linear *= gains
        rgb = np.where(linear <= .0031308, linear * 12.92, 1.055 * np.power(linear, 1/2.4) - .055)
        luma = np.clip(rgb[:,:,0] * .2126 + rgb[:,:,1] * .7152 + rgb[:,:,2] * .0722, 0, 1)
        shadow = 1 - luma
        lift = (r["shadows"] * shadow**2 * luma * .014 + r["highlights"] * luma**2 * shadow * .014
                + r["whites"] * luma**4 * .002 + r["blacks"] * shadow**4 * .002)
        rgb += lift[:,:,None]
        rgb = .45 + (rgb - .45) * (1 + r["contrast"] * .008)
        if r["saturation"]:
            grey = rgb[:,:,0] * .2126 + rgb[:,:,1] * .7152 + rgb[:,:,2] * .0722
            rgb = grey[:,:,None] + (rgb - grey[:,:,None]) * (1 + r["saturation"] / 100)
        result[top-y0:bottom-y0] = np.clip(rgb * 255 + .5, 0, 255).astype(np.uint8)
    return apply_detail(Image.fromarray(result), r)


def jpeg(image, quality=88, exif=b""):
    stream = io.BytesIO()
    image.save(stream, format="JPEG", quality=quality, exif=exif, icc_profile=SRGB)
    return stream.getvalue()


TILE = 512


def detail_tile(source, settings, column, row, step):
    """Render an aligned source-pixel tile. Include the neighbouring filter samples at its edges."""
    if step not in (1, 2, 4, 8, 16) or min(column, row) < 0:
        raise ValueError("Invalid zoom tile.")
    settings = recipe(settings)
    left, top, right, bottom = pixel_box(source.size, settings["crop"])
    span = TILE * step
    x0, y0 = max(left, column * span), max(top, row * span)
    x1, y1 = min(right, (column + 1) * span), min(bottom, (row + 1) * span)
    if x0 >= x1 or y0 >= y1:
        raise ValueError("Zoom tile is outside this crop.")
    margin = 16 if any(settings[k] for k in ("luminance", "color", "sharpen")) else 0
    padded = max(left, x0 - margin), max(top, y0 - margin), min(right, x1 + margin), min(bottom, y1 + margin)
    tile = render(source, settings, box=padded)
    if margin:
        tile = tile.crop((x0-padded[0], y0-padded[1], x1-padded[0], y1-padded[1]))
    if step > 1:
        tile = tile.resize((math.ceil(tile.width/step), math.ceil(tile.height/step)), Image.Resampling.LANCZOS)
    data = io.BytesIO()
    tile.save(data, "PNG", compress_level=2)
    return data.getvalue()


def histogram(image):
    sample = image.copy()
    sample.thumbnail((260, 180))
    return np.array(sample.histogram()).reshape((3, 64, 4)).sum(axis=2).tolist()


def auto_tone(source):
    """Estimate conservative automatic adjustments from a downsampled sRGB preview.

    This produces a non-destructive editing recipe; it never changes source pixels.
    """
    image = render(source, recipe())
    image.thumbnail((640, 640), Image.Resampling.BILINEAR)
    rgb = np.asarray(image, dtype=np.float32) / 255.0
    if rgb.size == 0:
        return recipe()
    lum = rgb[..., 0] * .2126 + rgb[..., 1] * .7152 + rgb[..., 2] * .0722
    p1, p10, p50, p90, p99 = np.percentile(lum, [1, 10, 50, 90, 99])
    # Avoid aggressive brightening of intentional low-key/night photographs.
    exposure = float(np.clip(np.log2(.43 / max(.08, p50)), -.85, .85))
    highlights = float(np.clip(-90 * max(0.0, p99 - .91) / .09, -45, 0))
    shadows = float(np.clip(80 * max(0.0, .22 - p10) / .22, 0, 38))
    whites = float(np.clip(20 * (.94 - p99), -12, 12))
    blacks = float(np.clip(-20 * p1, -12, 0))
    contrast = float(np.clip((.63 - (p90 - p10)) * 25, -10, 12))
    result = recipe()
    result.update(exposure=round(exposure, 2), highlights=round(highlights),
                  shadows=round(shadows), whites=round(whites),
                  blacks=round(blacks), contrast=round(contrast))
    return result
