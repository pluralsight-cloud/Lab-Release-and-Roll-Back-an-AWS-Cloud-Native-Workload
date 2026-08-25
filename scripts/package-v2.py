#!/usr/bin/env python3
"""Build the deterministic Lambda version 2 zip archive."""

import argparse
import zipfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FUNCTION_SOURCE = REPOSITORY_ROOT / "assets/function/v2/index.py"


def build_archive(output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    archive_entry = zipfile.ZipInfo("index.py", date_time=(1980, 1, 1, 0, 0, 0))
    archive_entry.compress_type = zipfile.ZIP_STORED
    archive_entry.create_system = 3
    archive_entry.external_attr = 0o100644 << 16

    with zipfile.ZipFile(output_path, mode="w") as archive:
        archive.writestr(archive_entry, FUNCTION_SOURCE.read_bytes())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    build_archive(arguments.output)
