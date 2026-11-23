"""Verified local Smart Crop models and explicit, install-time downloads.

Reading or locating models never accesses the network or creates directories.
Only the ``--download`` command installs files. Inference stays fully offline.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sys
import tempfile
from urllib.error import URLError
from urllib.request import urlopen


class ModelUnavailable(RuntimeError):
    """A required local model is missing, unreadable, or fails verification."""


@dataclass(frozen=True)
class ModelSpec:
    role: str
    filename: str
    size: int
    sha256: str
    source_path: str

    @property
    def url(self) -> str:
        return (
            "https://media.githubusercontent.com/media/opencv/opencv_zoo/"
            f"{SOURCE_COMMIT}/{self.source_path}/{self.filename}"
        )


SOURCE_COMMIT = "48b10fa505aeb51fbd127fc60a5c660040e823b7"
MODELS = (
    ModelSpec(
        "face", "face_detection_yunet_2023mar.onnx", 232589,
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        "models/face_detection_yunet",
    ),
    ModelSpec(
        "person", "object_detection_yolox_2022nov.onnx", 35858002,
        "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
        "models/object_detection_yolox",
    ),
)
_BLOCK_SIZE = 1024 * 1024


def model_directory(directory: Path | None = None) -> Path:
    """Resolve a model location without touching the filesystem."""
    if directory is not None:
        return Path(directory).expanduser()
    override = os.environ.get("FIELDROOM_MODEL_DIR")
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home and Path(data_home).is_absolute() else Path.home() / ".local/share"
    return base / "fieldroom/models"


def _validate(path: Path, spec: ModelSpec) -> None:
    """Verify content as well as size; an LFS pointer is not a usable model."""
    try:
        if not path.is_file():
            raise ModelUnavailable(f"Smart Crop model is missing: {path}")
        if path.stat().st_size != spec.size:
            raise ModelUnavailable(f"Smart Crop model has the wrong size: {path}")
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as stream:
            while block := stream.read(_BLOCK_SIZE):
                total += len(block)
                if total > spec.size:
                    raise ModelUnavailable(f"Smart Crop model changed during verification: {path}")
                digest.update(block)
        if total != spec.size or digest.hexdigest() != spec.sha256:
            raise ModelUnavailable(f"Smart Crop model failed its SHA-256 check: {path}")
    except OSError as exc:
        raise ModelUnavailable(f"Cannot read Smart Crop model {path}: {exc}") from exc


def locate_models(directory: Path | None = None) -> dict[str, Path]:
    """Return verified model paths. This function never downloads anything."""
    folder = model_directory(directory)
    found = {}
    for spec in MODELS:
        path = folder / spec.filename
        try:
            _validate(path, spec)
        except ModelUnavailable as exc:
            raise ModelUnavailable(
                f"{exc}. Run the Fieldroom installer again while connected to the internet, "
                "or copy verified offline models into this folder."
            ) from exc
        found[spec.role] = path
    return found


def _download(path: Path, spec: ModelSpec) -> None:
    """Bound the transfer and replace the destination only after verification."""
    temporary: Path | None = None
    try:
        with urlopen(spec.url, timeout=30) as response:
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    if int(length) != spec.size:
                        raise ModelUnavailable(f"Unexpected download size for {spec.filename}.")
                except ValueError as exc:
                    raise ModelUnavailable(f"Invalid download size for {spec.filename}.") from exc
            with tempfile.NamedTemporaryFile(
                prefix=f".{spec.filename}.", suffix=".part", dir=path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                total = 0
                digest = hashlib.sha256()
                while block := response.read(min(_BLOCK_SIZE, spec.size + 1 - total)):
                    total += len(block)
                    if total > spec.size:
                        raise ModelUnavailable(f"Download exceeded the expected size for {spec.filename}.")
                    digest.update(block)
                    stream.write(block)
                if total != spec.size or digest.hexdigest() != spec.sha256:
                    raise ModelUnavailable(f"Downloaded {spec.filename} failed verification; existing files were kept.")
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except (OSError, URLError) as exc:
        raise ModelUnavailable(f"Could not download {spec.filename}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def download_models(directory: Path | None = None) -> dict[str, Path]:
    """Explicit installation action; reuse already verified local weights."""
    folder = model_directory(directory)
    folder.mkdir(parents=True, exist_ok=True)
    for spec in MODELS:
        path = folder / spec.filename
        try:
            _validate(path, spec)
        except ModelUnavailable:
            print(f"Downloading {spec.filename} ({spec.size / 1_000_000:.2f} MB)…", flush=True)
            _download(path, spec)
        else:
            print(f"Verified existing {spec.filename}.", flush=True)
    return locate_models(folder)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install or check Fieldroom's offline Smart Crop models.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--download", action="store_true", help="Download missing or invalid models, then check the runtime.")
    mode.add_argument("--check", action="store_true", help="Check existing models and OpenCV without network access (the default).")
    parser.add_argument("--directory", type=Path, help="Use this model folder instead of FIELDROOM_MODEL_DIR or the per-user default.")
    args = parser.parse_args(argv)
    try:
        if args.download:
            download_models(args.directory)
        else:
            locate_models(args.directory)
        # Import after parsing so --help does not need image or GUI dependencies.
        from .detectors import check_runtime
        check_runtime(args.directory)
    except (ModelUnavailable, RuntimeError, ImportError, OSError) as exc:
        print(f"Smart Crop setup is not ready: {exc}", file=sys.stderr)
        return 1
    print(f"Smart Crop is ready for offline use. Models: {model_directory(args.directory)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
