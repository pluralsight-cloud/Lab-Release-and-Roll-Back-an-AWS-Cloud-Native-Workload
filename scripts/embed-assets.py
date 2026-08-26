#!/usr/bin/env python3
import argparse
import base64
import gzip
import re
from pathlib import Path


ASSETS = {
    "APPSPEC_GZIP_BASE64": Path("assets/appspec/release-v2.json"),
    "INVOKE_LOOP_GZIP_BASE64": Path("assets/helpers/invoke-loop.py"),
    "RECORD_OUTCOME_GZIP_BASE64": Path("assets/helpers/record-outcome.py"),
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Deterministically embed canonical learner assets in the template."
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    return parser.parse_args()


def embed_assets(repository_root):
    template_path = repository_root / "infrastructure/template.yaml"
    template = template_path.read_text(encoding="utf-8")

    for variable, asset_path in ASSETS.items():
        asset = (repository_root / asset_path).read_bytes()
        encoded = base64.b64encode(gzip.compress(asset, mtime=0)).decode("ascii")
        assignment = re.compile(
            rf"(?m)^(?P<prefix>[ \t]*{re.escape(variable)}=)'[^'\n]*'(?P<suffix>[ \t]*)$"
        )
        template, replacements = assignment.subn(
            rf"\g<prefix>'{encoded}'\g<suffix>",
            template,
        )
        if replacements != 1:
            raise SystemExit(
                f"expected exactly one single-quoted {variable} assignment; "
                f"found {replacements}"
            )

    template_path.write_text(template, encoding="utf-8")


if __name__ == "__main__":
    embed_assets(parse_args().repository_root.resolve())
