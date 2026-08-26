import hashlib
import importlib.util
import json
import re
import shlex
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


def extract_shell_function(script, function_name):
    match = re.search(
        rf"(?ms)^{re.escape(function_name)}\(\) \{{\n.*?^\}}$",
        script,
    )
    if match is None:
        raise AssertionError(f"missing {function_name} shell function")
    return match.group(0)


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

    def test_template_bootstraps_v2_after_the_v1_alias_using_the_pinned_archive(self):
        template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        resources = template["Resources"]
        workstation = resources["LabWorkstation"]
        role = resources["LabWorkstationRole"]
        user_data_node = workstation["Properties"]["UserData"]["Fn::Base64"]

        self.assertIn("DependsOn", workstation, "workstation must wait for the v1 alias")
        self.assertEqual(
            set(workstation["DependsOn"]),
            {
                "OrdersProdAlias",
                "OrdersErrorsAlarm",
                "LabPublicSubnetRouteTableAssociation",
            },
        )
        self.assertIn(
            "CreationPolicy",
            workstation,
            "workstation must signal that the v2 seed is ready",
        )
        self.assertEqual(
            workstation["CreationPolicy"]["ResourceSignal"]["Count"],
            1,
        )
        self.assertIsInstance(user_data_node, dict)
        self.assertIn("Fn::Sub", user_data_node)
        user_data = user_data_node["Fn::Sub"]

        policies = {
            policy["PolicyName"]: policy["PolicyDocument"]["Statement"]
            for policy in role["Properties"]["Policies"]
        }
        bootstrap_statements = policies["globomantics-orders-v2-bootstrap"]
        bootstrap_lambda_actions = bootstrap_statements[0]["Action"]
        self.assertEqual(
            set(
                bootstrap_lambda_actions
                if isinstance(bootstrap_lambda_actions, list)
                else [bootstrap_lambda_actions]
            ),
            {"lambda:GetFunction", "lambda:UpdateFunctionCode"},
        )
        self.assertEqual(
            bootstrap_statements[1]["Action"],
            "cloudwatch:GetMetricStatistics",
        )
        self.assertEqual(
            bootstrap_statements[2]["Action"],
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
        self.assertNotIn("lambda:GetFunction", learner_actions)
        self.assertNotIn("iam:DeleteRolePolicy", learner_actions)
        self.assertNotIn("cloudwatch:GetMetricStatistics", learner_actions)
        self.assertIn("cloudformation:SignalResource", learner_actions)

        archive_sha256 = hashlib.sha256(FUNCTION_ARCHIVE.read_bytes()).hexdigest()
        self.assertIn(archive_sha256, user_data)
        self.assertIn("assets/function/v2.zip", user_data)
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

    def test_bootstrap_failure_signal_classifies_the_last_lambda_update_error(self):
        template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        user_data = template["Resources"]["LabWorkstation"]["Properties"]["UserData"][
            "Fn::Base64"
        ]["Fn::Sub"]
        classifier = extract_shell_function(user_data, "classify_update_error")
        signaler = extract_shell_function(user_data, "signal_bootstrap")

        cases = {
            "You must specify a region. You can also configure your region": "lambda-no-region",
            "AccessDeniedException: not authorized to perform lambda:UpdateFunctionCode": "lambda-access-denied",
            "Unknown options: --dry-run": "lambda-cli-unsupported",
            "An error occurred (ResourceConflictException)": "lambda-update-timeout",
        }

        for error_message, expected_stage in cases.items():
            with self.subTest(expected_stage=expected_stage):
                diagnostic_script = f"""
set -euo pipefail
{classifier}
{signaler}
aws() {{ printf '%s\\n' "$*"; }}
INSTANCE_ID=i-diagnostic
STACK_NAME=diagnostic-stack
AWS_REGION=us-east-1
BOOTSTRAP_STAGE=$(classify_update_error {shlex.quote(error_message)})
signal_bootstrap FAILURE
"""
                result = subprocess.run(
                    ["bash", "-c", diagnostic_script],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    result.stdout.strip(),
                    "cloudformation signal-resource "
                    "--stack-name diagnostic-stack "
                    "--logical-resource-id LabWorkstation "
                    f"--unique-id i-diagnostic-{expected_stage} "
                    "--status FAILURE --region us-east-1 --no-cli-pager",
                )

    def test_bootstrap_verifies_alias_and_direct_v1_v2_behavior(self):
        template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        user_data = template["Resources"]["LabWorkstation"]["Properties"]["UserData"][
            "Fn::Base64"
        ]["Fn::Sub"]
        verifier = extract_shell_function(user_data, "verify_seed_state")

        fake_aws = r"""
aws() {
  case " $* " in
    *' lambda get-alias '*)
      printf '%s\n' '{"FunctionVersion":"1"}'
      ;;
    *' lambda invoke '*' --qualifier 1 '*)
      output_file="${!#}"
      printf '%s\n' '{"order_id":"order-1001","status":"confirmed","version":"v1"}' > "$output_file"
      printf '%s\n' '{"StatusCode":200,"ExecutedVersion":"1"}'
      ;;
    *' lambda invoke '*' --qualifier 2 '*)
      output_file="${!#}"
      if [ "$FAKE_SCENARIO" = 'bad-v2-required-field' ]; then
        printf '%s\n' '{"errorMessage":"Different failure.","errorType":"RuntimeError","requestId":"4e9ac3d2-a1d8-4d65-a477-7a3c4ef57c42","stackTrace":["  File \"/var/task/index.py\", line 3, in lambda_handler"]}' > "$output_file"
      else
        printf '%s\n' '{"errorMessage":"Simulated v2 order-processing failure.","errorType":"RuntimeError","requestId":"4e9ac3d2-a1d8-4d65-a477-7a3c4ef57c42","stackTrace":["  File \"/var/task/index.py\", line 3, in lambda_handler"]}' > "$output_file"
      fi
      if [ "$FAKE_SCENARIO" = 'bad-v2-metadata' ]; then
        printf '%s\n' '{"StatusCode":200,"ExecutedVersion":"2"}'
      else
        printf '%s\n' '{"StatusCode":200,"FunctionError":"Unhandled","ExecutedVersion":"2"}'
      fi
      ;;
    *)
      printf '%s\n' "unexpected fake AWS call: $*" >&2
      return 64
      ;;
  esac
}
"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            lab_root = Path(temporary_directory) / "lab"
            state_directory = lab_root / "state"
            state_directory.mkdir(parents=True)

            for scenario, expected_return_code in (
                ("healthy", 0),
                ("bad-v2-metadata", 1),
                ("bad-v2-required-field", 1),
            ):
                with self.subTest(scenario=scenario):
                    for path in state_directory.iterdir():
                        path.unlink()

                    diagnostic_script = f"""
set -euo pipefail
{verifier}
{fake_aws}
LAB_ROOT={shlex.quote(str(lab_root))}
FUNCTION_NAME=globomantics-orders
FAKE_SCENARIO={shlex.quote(scenario)}
verify_seed_state
"""
                    result = subprocess.run(
                        ["bash", "-c", diagnostic_script],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        result.returncode,
                        expected_return_code,
                        result.stderr,
                    )

                    if expected_return_code == 0:
                        self.assertEqual(
                            json.loads((state_directory / "prod-alias.json").read_text()),
                            {"FunctionVersion": "1"},
                        )
                        self.assertEqual(
                            json.loads((state_directory / "v1-invoke.json").read_text()),
                            {"StatusCode": 200, "ExecutedVersion": "1"},
                        )
                        self.assertEqual(
                            json.loads((state_directory / "v2-invoke.json").read_text()),
                            {
                                "StatusCode": 200,
                                "FunctionError": "Unhandled",
                                "ExecutedVersion": "2",
                            },
                        )


if __name__ == "__main__":
    unittest.main()
