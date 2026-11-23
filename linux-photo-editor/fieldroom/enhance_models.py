"""Explicit installation and integrity checks for offline AI detail models.

Locating or using a model never downloads anything. Only ``--download`` installs
the pinned, independently licensed pretrained files listed here.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
from urllib.error import URLError
from urllib.request import urlopen

from .models import model_directory


class EnhancementUnavailable(RuntimeError):
    """The requested neural enhancement cannot run with this installation."""


@dataclass(frozen=True)
class DetailModel:
    role: str
    filename: str
    size: int
    sha256: str
    url: str
    label: str


MODELS = (
    DetailModel(
        "denoise", "dncnn_color.onnx", 2680354,
        "0dbe1d53877efb63019416b000b6f725fd0b5e706305f11954b0bc8f53513a84",
        "https://github.com/ikeno-web/npuscale/releases/download/v1.2/dncnn_color.onnx",
        "DnCNN color-blind denoising",
    ),
    DetailModel(
        "sharpen", "super-resolution-10.onnx", 240078,
        "85f36ff88cc504a24af5e0602148bc56a8aa09a58eca8c0da2756f3e8186035e",
        "https://media.githubusercontent.com/media/onnx/models/"
        "c5cae0f1942c8d7727372c7b1f6b485aa39e4421/validated/vision/"
        "super_resolution/sub_pixel_cnn_2016/model/super-resolution-10.onnx",
        "ESPCN detail restoration",
    ),
)
_BLOCK = 1024 * 1024
_verified: dict[tuple, bool] = {}
_verification_lock = threading.Lock()


def validate(path: Path, spec: DetailModel) -> None:
    """Reject LFS pointers and modified weights before creating a runtime."""
    try:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, spec.sha256)
        if stat.st_size != spec.size or not path.is_file():
            raise EnhancementUnavailable(f"AI detail model has the wrong size: {spec.filename}.")
        with _verification_lock:
            if key in _verified:
                return
            digest = hashlib.sha256()
            total = 0
            with path.open("rb") as stream:
                while block := stream.read(_BLOCK):
                    total += len(block)
                    if total > spec.size:
                        raise EnhancementUnavailable(f"AI detail model changed: {spec.filename}.")
                    digest.update(block)
            if total != spec.size or digest.hexdigest() != spec.sha256:
                raise EnhancementUnavailable(f"AI detail model failed its integrity check: {spec.filename}.")
            if len(_verified) > 16:
                _verified.clear()
            _verified[key] = True
    except OSError as exc:
        raise EnhancementUnavailable(f"AI detail model is missing or unreadable: {spec.filename}.") from exc


def locate_models(directory: Path | None = None, roles=None) -> dict[str, Path]:
    folder = model_directory(directory)
    wanted = set(roles) if roles is not None else {spec.role for spec in MODELS}
    found = {}
    for spec in MODELS:
        if spec.role not in wanted:
            continue
        path = folder / spec.filename
        try:
            validate(path, spec)
        except EnhancementUnavailable as exc:
            raise EnhancementUnavailable(
                f"{exc} Run the updated Fieldroom installer while online, or copy the verified offline models into {folder}."
            ) from exc
        found[spec.role] = path
    if wanted - found.keys():
        raise ValueError("Unknown AI detail model.")
    return found


def _download(path: Path, spec: DetailModel) -> None:
    temporary = None
    try:
        with urlopen(spec.url, timeout=30) as response:
            length = response.headers.get("Content-Length")
            if length is not None and int(length) != spec.size:
                raise EnhancementUnavailable(f"Unexpected download size for {spec.filename}.")
            with tempfile.NamedTemporaryFile(prefix=f".{spec.filename}.", suffix=".part", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                total = 0
                digest = hashlib.sha256()
                while block := response.read(min(_BLOCK, spec.size + 1 - total)):
                    total += len(block)
                    if total > spec.size:
                        raise EnhancementUnavailable(f"Download exceeded the expected size for {spec.filename}.")
                    digest.update(block)
                    stream.write(block)
                if total != spec.size or digest.hexdigest() != spec.sha256:
                    raise EnhancementUnavailable(f"Downloaded {spec.filename} failed verification; existing files were kept.")
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except (OSError, URLError, ValueError) as exc:
        raise EnhancementUnavailable(f"Could not install {spec.filename}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def download_models(directory: Path | None = None) -> dict[str, Path]:
    folder = model_directory(directory)
    folder.mkdir(parents=True, exist_ok=True)
    for spec in MODELS:
        path = folder / spec.filename
        try:
            validate(path, spec)
        except EnhancementUnavailable:
            print(f"Downloading {spec.label} ({spec.size / 1_000_000:.2f} MB)…", flush=True)
            _download(path, spec)
        else:
            print(f"Verified existing {spec.filename}.", flush=True)
    return locate_models(folder)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install or check Fieldroom's offline AI denoise and detail models.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--download", action="store_true", help="Install verified model files, then run a real inference check.")
    mode.add_argument("--check", action="store_true", help="Check the existing installation without network access.")
    parser.add_argument("--directory", type=Path, help="Override FIELDROOM_MODEL_DIR for this command.")
    args = parser.parse_args(argv)
    try:
        if args.download:
            download_models(args.directory)
        else:
            locate_models(args.directory)
        from .enhance import check_runtime
        check_runtime(args.directory)
    except (EnhancementUnavailable, RuntimeError, ImportError, OSError) as exc:
        print(f"AI detail setup is not ready: {exc}", file=sys.stderr)
        return 1
    print(f"Offline AI denoise and detail are ready. Models: {model_directory(args.directory)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
