"""Lossless CSV rotation with verified gzip publication."""
from __future__ import annotations

import gzip
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class RotationResult:
    archive_path: str
    compressed: bool


def _archive_paths(path: str, utc_day: date) -> tuple[str, str]:
    root, extension = os.path.splitext(path)
    dated = f"{root}-{utc_day:%Y%m%d}{extension}"
    suffix = 0
    while True:
        raw = dated if suffix == 0 else f"{dated}.{suffix}"
        compressed = (
            f"{dated}.gz" if suffix == 0 else f"{dated}.gz.{suffix}")
        if not os.path.exists(raw) and not os.path.exists(compressed):
            return raw, compressed
        suffix += 1


def rotate_csv_gzip(path: str, utc_day: date) -> RotationResult:
    """Move *path* aside, publish a verified gzip, and never lose raw data."""
    raw_path, compressed_path = _archive_paths(path, utc_day)
    os.replace(path, raw_path)
    directory = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp.gz",
        dir=directory,
    )
    os.close(fd)
    try:
        with open(raw_path, "rb") as source, gzip.open(
                temp_path, "wb") as target:
            shutil.copyfileobj(source, target)
        with gzip.open(temp_path, "rb") as check:
            while check.read(1024 * 1024):
                pass
        os.replace(temp_path, compressed_path)
        temp_path = ""
        os.remove(raw_path)
    except (OSError, gzip.BadGzipFile):
        if temp_path:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass
        return RotationResult(raw_path, False)
    return RotationResult(compressed_path, True)
