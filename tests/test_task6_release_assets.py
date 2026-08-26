import base64
import gzip
import json
import re
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APPSPEC = REPOSITORY_ROOT / "assets/appspec/release-v2.json"
TEMPLATE = REPOSITORY_ROOT / "infrastructure/template.yaml"

EXPECTED_APPSPEC = {
    "version": 0.0,
    "Resources": [
        {
            "OrdersFunction": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Name": "globomantics-orders",
                    "Alias": "prod",
                    "CurrentVersion": "1",
                    "TargetVersion": "TARGET_VERSION",
                },
            }
        }
    ],
}


def user_data():
    template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    return template["Resources"]["LabWorkstation"]["Properties"]["UserData"][
        "Fn::Base64"
    ]["Fn::Sub"]


class Task6ReleaseAssetTests(unittest.TestCase):
    def test_source_appspec_is_the_learner_finalizable_release_contract(self):
        self.assertTrue(APPSPEC.is_file(), "missing learner release AppSpec source")

        source = APPSPEC.read_bytes()
        self.assertTrue(source.endswith(b"\n"), "AppSpec source needs a trailing newline")
        parsed = json.loads(source)
        self.assertEqual(parsed, EXPECTED_APPSPEC)
        self.assertNotIn("S3Location", json.dumps(parsed))

    def test_template_embeds_the_exact_appspec_source_bytes(self):
        self.assertTrue(APPSPEC.is_file(), "missing learner release AppSpec source")

        match = re.search(
            r"(?m)^APPSPEC_GZIP_BASE64='([A-Za-z0-9+/=]+)'$",
            user_data(),
        )
        self.assertIsNotNone(match, "workstation does not embed the release AppSpec")
        embedded = gzip.decompress(base64.b64decode(match.group(1)))

        self.assertEqual(embedded, APPSPEC.read_bytes())

    def test_userdata_creates_learner_directories_and_installs_appspec_permissions(self):
        script = user_data()
        directory_install = re.search(
            r"(?ms)^\s*install -d -o cloud_user -g cloud_user \\\n(?P<paths>(?:\s+\"\$LAB_ROOT/[^\"]+\"(?: \\\n)?)+)$",
            script,
        )
        self.assertIsNotNone(
            directory_install,
            "bootstrap must create learner-owned lab directories",
        )
        paths = directory_install.group("paths")
        self.assertIn('"$LAB_ROOT/appspec"', paths)
        self.assertIn('"$LAB_ROOT/output"', paths)

        appspec_path = '"$LAB_ROOT/appspec/release-v2.json"'
        self.assertIn(
            'base64 -d <<<"$APPSPEC_GZIP_BASE64" | gzip -d > ' + appspec_path,
            script,
        )
        self.assertIn(f"chown cloud_user:cloud_user {appspec_path}", script)
        self.assertIn(f"chmod 0644 {appspec_path}", script)


if __name__ == "__main__":
    unittest.main()
