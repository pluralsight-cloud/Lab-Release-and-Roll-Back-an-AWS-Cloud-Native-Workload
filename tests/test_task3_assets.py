import base64
import hashlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FUNCTION_SOURCE = REPOSITORY_ROOT / "assets/function/v2/index.py"
FUNCTION_ARCHIVE = REPOSITORY_ROOT / "assets/function/v2.zip"
FUNCTION_CHECKSUM = REPOSITORY_ROOT / "assets/function/v2.zip.sha256"
PACKAGE_SCRIPT = REPOSITORY_ROOT / "scripts/package-v2.py"
TEMPLATE = REPOSITORY_ROOT / "infrastructure/template.yaml"


class Task3AssetTests(unittest.TestCase):
    def test_v2_handler_fails_every_invoke_with_the_expected_diagnostic(self):
        self.assertTrue(FUNCTION_SOURCE.is_file(), "missing deterministic v2 source")

        spec = importlib.util.spec_from_file_location("orders_v2", FUNCTION_SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with self.assertRaisesRegex(
            RuntimeError,
            r"^Simulated v2 order-processing failure\.$",
        ):
            module.lambda_handler({}, None)

    def test_committed_v2_zip_is_reproducible_and_checksum_pinned(self):
        self.assertTrue(PACKAGE_SCRIPT.is_file(), "missing deterministic packaging script")
        self.assertTrue(FUNCTION_ARCHIVE.is_file(), "missing committed v2 zip")
        self.assertTrue(FUNCTION_CHECKSUM.is_file(), "missing committed v2 checksum")

        with tempfile.TemporaryDirectory() as temporary_directory:
            rebuilt_archive = Path(temporary_directory) / "v2.zip"
            subprocess.run(
                [sys.executable, str(PACKAGE_SCRIPT), "--output", str(rebuilt_archive)],
                cwd=REPOSITORY_ROOT,
                check=True,
            )
            self.assertEqual(FUNCTION_ARCHIVE.read_bytes(), rebuilt_archive.read_bytes())

        archive_bytes = FUNCTION_ARCHIVE.read_bytes()
        archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        checksum_record = FUNCTION_CHECKSUM.read_text(encoding="utf-8").strip()
        self.assertEqual(checksum_record, f"{archive_sha256}  v2.zip")

        with zipfile.ZipFile(FUNCTION_ARCHIVE) as archive:
            self.assertEqual(archive.namelist(), ["index.py"])
            archive_entry = archive.getinfo("index.py")
            self.assertEqual(archive_entry.date_time, (1980, 1, 1, 0, 0, 0))
            self.assertEqual(archive.read("index.py"), FUNCTION_SOURCE.read_bytes())

    def test_template_bootstraps_v2_after_the_v1_alias_without_egress(self):
        template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        resources = template["Resources"]
        workstation = resources["LabWorkstation"]
        role = resources["LabWorkstationRole"]
        user_data_node = workstation["Properties"]["UserData"]["Fn::Base64"]

        self.assertIn("DependsOn", workstation, "workstation must wait for the v1 alias")
        self.assertEqual(
            set(workstation["DependsOn"]),
            {"OrdersProdAlias", "LabPublicSubnetRouteTableAssociation"},
        )
        self.assertIn(
            "CreationPolicy",
            workstation,
            "workstation must signal that the v2 seed is ready",
        )
        self.assertEqual(
            workstation["CreationPolicy"]["ResourceSignal"],
            {"Count": 1, "Timeout": "PT10M"},
        )
        self.assertIsInstance(user_data_node, dict)
        self.assertIn("Fn::Sub", user_data_node)
        user_data = user_data_node["Fn::Sub"]

        policies = {
            policy["PolicyName"]: policy["PolicyDocument"]["Statement"]
            for policy in role["Properties"]["Policies"]
        }
        bootstrap_statements = policies["globomantics-orders-v2-bootstrap"]
        self.assertEqual(
            bootstrap_statements[0]["Action"],
            "lambda:UpdateFunctionCode",
        )
        self.assertEqual(
            bootstrap_statements[1]["Action"],
            "iam:DeleteRolePolicy",
        )

        learner_actions = {
            action
            for statement in policies["globomantics-orders-workstation-access"]
            for action in (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
        }
        self.assertNotIn("lambda:UpdateFunctionCode", learner_actions)
        self.assertNotIn("iam:DeleteRolePolicy", learner_actions)
        self.assertIn("cloudformation:SignalResource", learner_actions)

        archive_base64 = base64.b64encode(FUNCTION_ARCHIVE.read_bytes()).decode("ascii")
        archive_sha256 = hashlib.sha256(FUNCTION_ARCHIVE.read_bytes()).hexdigest()
        self.assertIn(archive_base64, user_data)
        self.assertIn(archive_sha256, user_data)
        self.assertIn("LAB_ROOT=/home/cloud_user/lab", user_data)
        self.assertIn("V2_ARCHIVE=$LAB_ROOT/assets/function/v2.zip", user_data)
        self.assertIn("update-function-code", user_data)
        self.assertIn("--dry-run", user_data)
        self.assertIn("--zip-file", user_data)
        self.assertIn('"fileb://$V2_ARCHIVE"', user_data)
        self.assertIn("--publish", user_data)
        self.assertIn("--query Version", user_data)
        self.assertIn("delete-role-policy", user_data)
        self.assertIn("globomantics-orders-v2-bootstrap", user_data)
        self.assertIn("$LAB_ROOT/state/v2-version.txt", user_data)
        self.assertIn("signal-resource", user_data)
        self.assertIn("FAILURE", user_data)
        self.assertIn("SUCCESS", user_data)

        self.assertEqual(template["Outputs"]["OrdersV2Version"]["Value"], "2")


if __name__ == "__main__":
    unittest.main()
