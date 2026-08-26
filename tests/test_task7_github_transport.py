import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPOSITORY_ROOT / "infrastructure/template.yaml"
PINNED_COMMIT = "7106a715f0a404be6cedaa3635eb6135e4883fbd"
ASSET_BASE_URL = (
    "https://raw.githubusercontent.com/pluralsight-cloud/"
    "Lab-Release-and-Roll-Back-an-AWS-Cloud-Native-Workload/"
    + PINNED_COMMIT
)
ASSETS = (
    (
        "assets/function/v2.zip",
        "2027a77a46868f6ac0a806d6cf0c5971fddcc72e8196009a507248fedcce72de",
        "/home/cloud_user/lab/assets/function/v2.zip",
        "0644",
    ),
    (
        "assets/appspec/release-v2.json",
        "f77f57264120e0bbc439d490d02cf887392ce0da1316985dfe9887f8b74b7c9c",
        "/home/cloud_user/lab/appspec/release-v2.json",
        "0644",
    ),
    (
        "assets/helpers/invoke-loop.py",
        "3dbfc959b289b748bd4d5be3415f1ac65f5a8a7a243a33af9cd78dd5201ef544",
        "/home/cloud_user/lab/bin/invoke-loop",
        "0755",
    ),
    (
        "assets/helpers/record-outcome.py",
        "90aadab2525ac5843f10a8e55e295ebd922c47db401296c88a0237e7ada26b9c",
        "/home/cloud_user/lab/bin/record-outcome",
        "0755",
    ),
)


def user_data():
    template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    return template["Resources"]["LabWorkstation"]["Properties"]["UserData"][
        "Fn::Base64"
    ]["Fn::Sub"]


def render_user_data_for_cloudformation(script):
    substitutions = {
        "AWS::StackName": "test-stack",
        "AWS::Region": "us-east-1",
    }
    unknown_tokens = []

    def replace_token(match):
        token = match.group(1)
        if token.startswith("!"):
            return "${" + token[1:] + "}"
        if token not in substitutions:
            unknown_tokens.append(token)
            return match.group(0)
        return substitutions[token]

    rendered = re.sub(r"\$\{([^}]+)\}", replace_token, script)
    return rendered, unknown_tokens


def extract_shell_function(script, function_name):
    match = re.search(
        rf"(?ms)^{re.escape(function_name)}\(\) \{{\n.*?^\}}$",
        script,
    )
    if match is None:
        raise AssertionError(f"missing {function_name} shell function")
    return match.group(0)


class Task7GitHubTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temporary_root = Path(self.temporary_directory.name)
        self.fake_bin = self.temporary_root / "bin"
        self.fake_bin.mkdir()
        self.attempt_log = self.temporary_root / "curl-attempts.log"
        self.chown_log = self.temporary_root / "chown.log"
        self._write_fake_curl()
        self._write_fake_chown()
        self._write_fake_sha256sum()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _write_fake_curl(self):
        (self.fake_bin / "curl").write_text(
            """#!/bin/bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_CURL_ATTEMPTS"
if [ "${FAKE_CURL_FAIL_FIRST:-false}" = true ] && [ ! -e "$FAKE_CURL_FAILURE_STATE" ]; then
  touch "$FAKE_CURL_FAILURE_STATE"
  exit 22
fi
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o)
      output="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
cp "$FAKE_CURL_SOURCE" "$output"
""",
            encoding="utf-8",
        )
        (self.fake_bin / "curl").chmod(0o755)

    def _write_fake_chown(self):
        (self.fake_bin / "chown").write_text(
            """#!/bin/bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_CHOWN_LOG"
""",
            encoding="utf-8",
        )
        (self.fake_bin / "chown").chmod(0o755)

    def _write_fake_sha256sum(self):
        (self.fake_bin / "sha256sum").write_text(
            """#!/usr/bin/env python3
import hashlib
import sys

if sys.argv[1:] != ["--check", "--status"]:
    raise SystemExit(64)
expected_sha, path = sys.stdin.read().strip().split("  ", 1)
actual_sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
raise SystemExit(0 if actual_sha == expected_sha else 1)
""",
            encoding="utf-8",
        )
        (self.fake_bin / "sha256sum").chmod(0o755)

    def run_download(self, asset, fixture, destination, *, fail_first=False):
        fixture_path = self.temporary_root / "fixture"
        fixture_path.write_bytes(fixture)
        function = extract_shell_function(user_data(), "download_asset")
        environment = {
            **os.environ,
            "ASSET_BASE_URL": ASSET_BASE_URL,
            "PATH": f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_CHOWN_LOG": str(self.chown_log),
            "FAKE_CURL_ATTEMPTS": str(self.attempt_log),
            "FAKE_CURL_FAILURE_STATE": str(self.temporary_root / "first-failure"),
            "FAKE_CURL_FAIL_FIRST": str(fail_first).lower(),
            "FAKE_CURL_SOURCE": str(fixture_path),
        }
        command = f"""
set -u
{function}
sleep() {{ :; }}
if download_asset {asset[0]!r} {asset[1]!r} {str(destination)!r} {asset[3]!r}; then
  exit 0
fi
exit 1
"""
        return subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_template_declares_the_pinned_raw_objects_without_embedded_assets(self):
        script = user_data()

        self.assertIn(f"ASSET_BASE_URL={ASSET_BASE_URL}", script)
        for asset_name, expected_sha, _, _ in ASSETS:
            self.assertIn(asset_name, script)
            self.assertIn(expected_sha, script)
        for embedded_name in (
            "INVOKE_LOOP_GZIP_BASE64",
            "RECORD_OUTCOME_GZIP_BASE64",
            "APPSPEC_GZIP_BASE64",
            "V2_ZIP_BASE64",
        ):
            self.assertNotIn(embedded_name, script)

    def test_userdata_has_no_unknown_cloudformation_substitutions(self):
        rendered, unknown_tokens = render_user_data_for_cloudformation(user_data())

        self.assertEqual(unknown_tokens, [])
        self.assertIn("STACK_NAME=test-stack", rendered)
        self.assertIn("AWS_REGION=us-east-1", rendered)

    def test_renderer_preserves_deliberate_cloudformation_literal_escapes(self):
        rendered, unknown_tokens = render_user_data_for_cloudformation(
            "literal=${!destination} stack=${AWS::StackName}"
        )

        self.assertEqual(unknown_tokens, [])
        self.assertEqual(rendered, "literal=${destination} stack=test-stack")

    def test_download_asset_installs_verified_bytes_atomically(self):
        asset = ASSETS[0]
        destination = self.temporary_root / "lab/assets/function/v2.zip"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"old learner archive")
        fixture = (REPOSITORY_ROOT / asset[0]).read_bytes()

        result = self.run_download(asset, fixture, destination)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(destination.read_bytes(), fixture)
        self.assertFalse(destination.with_name(destination.name + ".download").exists())

    def test_download_asset_retries_transport_failure_then_succeeds(self):
        asset = ASSETS[1]
        destination = self.temporary_root / "lab/appspec/release-v2.json"
        destination.parent.mkdir(parents=True)
        fixture = (REPOSITORY_ROOT / asset[0]).read_bytes()

        result = self.run_download(asset, fixture, destination, fail_first=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(destination.read_bytes(), fixture)
        self.assertEqual(self.attempt_log.read_text(encoding="utf-8").count("\n"), 2)

    def test_download_asset_rejects_checksum_mismatch_without_replacing_destination(self):
        asset = ASSETS[2]
        destination = self.temporary_root / "lab/bin/invoke-loop"
        destination.parent.mkdir(parents=True)
        original = b"existing learner helper\n"
        destination.write_bytes(original)

        result = self.run_download(asset, b"untrusted helper bytes\n", destination)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(destination.read_bytes(), original)
        self.assertFalse(destination.with_name(destination.name + ".download").exists())
        self.assertEqual(self.attempt_log.read_text(encoding="utf-8").count("\n"), 3)

    def test_bootstrap_downloads_all_four_assets_before_their_first_use(self):
        script = user_data()
        download_stage = script.index("BOOTSTRAP_STAGE=asset-download")
        download_calls = [
            script.index(f'download_asset "${variable}_ASSET"')
            for variable in ("V2", "APPSPEC", "INVOKE_LOOP", "RECORD_OUTCOME")
        ]
        first_uses = (
            script.index('aws sts get-caller-identity >/dev/null'),
            script.index('"fileb://$V2_ARCHIVE"'),
        )

        self.assertTrue(all(download_stage < call for call in download_calls))
        self.assertLess(max(download_calls), min(first_uses))

    def test_transport_preserves_learner_paths_ownership_and_modes(self):
        for asset in ASSETS:
            destination = self.temporary_root / asset[2].lstrip("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            fixture = (REPOSITORY_ROOT / asset[0]).read_bytes()

            result = self.run_download(asset, fixture, destination)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(destination.read_bytes(), fixture)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), int(asset[3], 8))

        self.assertEqual(
            self.chown_log.read_text(encoding="utf-8").splitlines(),
            [
                f"cloud_user:cloud_user {self.temporary_root / asset[2].lstrip('/')}"
                f".download"
                for asset in ASSETS
            ],
        )

    def test_userdata_retains_at_least_three_kibibytes_of_headroom(self):
        self.assertLessEqual(len(user_data().encode("utf-8")), 16_384 - 3_072)


if __name__ == "__main__":
    unittest.main()
