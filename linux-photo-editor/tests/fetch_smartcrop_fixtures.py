"""Fetch public, hash-pinned test photos before the offline model tests run."""
from pathlib import Path
import hashlib
import sys
from urllib.request import urlopen

FIXTURES = (
    ("basketball1.png", "https://raw.githubusercontent.com/opencv/opencv/master/samples/data/basketball1.png",
     "ba06f6701f7260998b430c39b6557f775497e6ce7b1a74f0b7ea6af371bf54a6"),
    ("messi5.jpg", "https://raw.githubusercontent.com/opencv/opencv/master/samples/data/messi5.jpg",
     "1d570e49654e84c7a943918537bd9e5e1ef82920152e147c834006e235be97c9"),
    ("dog.jpg", "https://raw.githubusercontent.com/Megvii-BaseDetection/YOLOX/main/assets/dog.jpg",
     "5a9522051c3cec2bbd2f6323fccba32e8fbf3ddcc2b3e2fd46b04c720bc6f866"),
)


def main():
    folder = Path(sys.argv[1])
    folder.mkdir(parents=True, exist_ok=True)
    for name, url, expected in FIXTURES:
        path = folder / name
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
            continue
        with urlopen(url, timeout=30) as response:
            data = response.read(1_000_000)
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError(f"Test photo checksum mismatch: {name}")
        path.write_bytes(data)
        print(f"Verified {name}")


if __name__ == "__main__":
    main()
