import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INVOKE_LOOP = REPOSITORY_ROOT / "assets/helpers/invoke-loop.py"
TEMPLATE = REPOSITORY_ROOT / "infrastructure/template.yaml"


def load_invoke_loop_module():
    spec = importlib.util.spec_from_file_location("invoke_loop", INVOKE_LOOP)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Task5InvokeLoopTests(unittest.TestCase):
    def run_loop(self, responses):
        self.assertTrue(INVOKE_LOOP.is_file(), "missing learner invoke-loop helper")

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            fake_bin = temporary_root / "bin"
            fake_bin.mkdir()
            fake_aws = fake_bin / "aws"
            fake_aws.write_text(
                """#!/usr/bin/env python3
import json
import os
import sys

responses = json.loads(os.environ["FAKE_AWS_RESPONSES"])
counter_path = os.environ["FAKE_AWS_COUNTER"]
try:
    index = int(open(counter_path, encoding="utf-8").read())
except FileNotFoundError:
    index = 0
open(counter_path, "w", encoding="utf-8").write(str(index + 1))

response = responses[index]
payload_path = sys.argv[-1]
open(payload_path, "w", encoding="utf-8").write(json.dumps(response["payload"]))
print(json.dumps(response["metadata"]))
raise SystemExit(response.get("exit_code", 0))
""",
                encoding="utf-8",
            )
            fake_aws.chmod(0o755)

            evidence_path = temporary_root / "invoke-loop.jsonl"
            counter_path = temporary_root / "counter.txt"
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "FAKE_AWS_RESPONSES": json.dumps(responses),
                "FAKE_AWS_COUNTER": str(counter_path),
            }
            result = subprocess.run(
                [
                    "python3",
                    str(INVOKE_LOOP),
                    "--count",
                    str(len(responses)),
                    "--interval",
                    "0",
                    "--evidence-file",
                    str(evidence_path),
                ],
                capture_output=True,
                text=True,
                env=environment,
            )
            evidence = []
            if evidence_path.exists():
                evidence = [
                    json.loads(line)
                    for line in evidence_path.read_text(encoding="utf-8").splitlines()
                ]
            calls = int(counter_path.read_text(encoding="utf-8")) if counter_path.exists() else 0
            return result, evidence, calls

    def test_defaults_are_sixty_invocations_two_seconds_and_output_directory(self):
        module = load_invoke_loop_module()
        with mock.patch.object(sys, "argv", [str(INVOKE_LOOP)]):
            args = module.parse_args()
        self.assertEqual(args.count, 60)
        self.assertEqual(args.interval, 2.0)
        self.assertEqual(
            args.evidence_file,
            Path("/home/cloud_user/lab/output/invoke-loop.jsonl"),
        )

    def test_exit_zero_function_error_is_counted_and_does_not_stop_the_loop(self):
        healthy = {
            "metadata": {"StatusCode": 200, "ExecutedVersion": "1"},
            "payload": {
                "order_id": "order-1001",
                "status": "confirmed",
                "version": "v1",
            },
        }
        failing_v2 = {
            "metadata": {
                "StatusCode": 200,
                "FunctionError": "Unhandled",
                "ExecutedVersion": "2",
            },
            "payload": {
                "errorMessage": "Simulated v2 order-processing failure.",
                "errorType": "RuntimeError",
            },
            "exit_code": 0,
        }

        result, evidence, calls = self.run_loop([healthy, failing_v2, healthy])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, 3, "expected function failure must not stop traffic")
        self.assertIn(
            "v2 failure 002: FunctionError=Unhandled ExecutedVersion=2 "
            "RuntimeError: Simulated v2 order-processing failure.",
            result.stdout,
        )
        self.assertIn(
            "Summary: total=3 v1=2 v2=1 function_errors=1 other=0",
            result.stdout,
        )
        self.assertEqual(
            evidence[1],
            {
                "cli_exit_code": 0,
                "executed_version": "2",
                "function_error": "Unhandled",
                "invocation": 2,
                "payload": {
                    "errorMessage": "Simulated v2 order-processing failure.",
                    "errorType": "RuntimeError",
                },
            },
        )

    def test_all_v1_sample_finishes_with_a_clear_retry_diagnostic(self):
        healthy = {
            "metadata": {"StatusCode": 200, "ExecutedVersion": "1"},
            "payload": {
                "order_id": "order-1001",
                "status": "confirmed",
                "version": "v1",
            },
        }

        result, evidence, calls = self.run_loop([healthy, healthy, healthy])

        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, 3)
        self.assertEqual(len(evidence), 3)
        self.assertIn(
            "No failing v2 response was sampled; run the helper again.",
            result.stderr,
        )

    def test_unexpected_v1_payload_fails_after_recording_evidence(self):
        unexpected_v1 = {
            "metadata": {"StatusCode": 200, "ExecutedVersion": "1"},
            "payload": {
                "order_id": "order-9999",
                "status": "confirmed",
                "version": "v1",
            },
        }

        result, evidence, calls = self.run_loop([unexpected_v1])

        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, 1)
        self.assertEqual(evidence[0]["payload"], unexpected_v1["payload"])
        self.assertIn("unexpected v1 payload", result.stderr.lower())

    def test_unexpected_v2_error_payload_fails_after_recording_evidence(self):
        unexpected_v2 = {
            "metadata": {
                "StatusCode": 200,
                "FunctionError": "Unhandled",
                "ExecutedVersion": "2",
            },
            "payload": {
                "errorMessage": "A different failure.",
                "errorType": "RuntimeError",
            },
        }

        result, evidence, calls = self.run_loop([unexpected_v2])

        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, 1)
        self.assertEqual(evidence[0]["payload"], unexpected_v2["payload"])
        self.assertIn("unexpected v2 error payload", result.stderr.lower())

    def test_transport_failure_stops_immediately_and_is_distinct_from_function_error(self):
        transport_failure = {
            "metadata": {
                "StatusCode": 200,
                "FunctionError": "Unhandled",
                "ExecutedVersion": "2",
            },
            "payload": {
                "errorMessage": "Simulated v2 order-processing failure.",
                "errorType": "RuntimeError",
            },
            "exit_code": 42,
        }
        healthy = {
            "metadata": {"StatusCode": 200, "ExecutedVersion": "1"},
            "payload": {
                "order_id": "order-1001",
                "status": "confirmed",
                "version": "v1",
            },
        }

        result, evidence, calls = self.run_loop([transport_failure, healthy])

        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, 1)
        self.assertEqual(evidence, [])
        self.assertIn("AWS CLI invocation 1 failed", result.stderr)
        self.assertNotIn("v2 failure", result.stdout)

    def test_progress_is_bounded_to_every_ten_invocations_and_completion(self):
        healthy = {
            "metadata": {"StatusCode": 200, "ExecutedVersion": "1"},
            "payload": {
                "order_id": "order-1001",
                "status": "confirmed",
                "version": "v1",
            },
        }

        result, _, calls = self.run_loop([healthy] * 21)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, 21)
        progress = [line for line in result.stdout.splitlines() if line.startswith("Progress")]
        self.assertEqual(
            [line.split(":", 1)[0] for line in progress],
            ["Progress 010/021", "Progress 020/021", "Progress 021/021"],
        )

    def test_alarm_uses_the_complete_prod_alias_dimensions(self):
        template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        metrics = template["Resources"]["OrdersErrorsAlarm"]["Properties"]["Metrics"]
        dimensions = [
            metric["MetricStat"]["Metric"]["Dimensions"]
            for metric in metrics
            if "MetricStat" in metric
        ]

        self.assertEqual(
            dimensions,
            [
                [
                    {
                        "Name": "FunctionName",
                        "Value": {"Ref": "OrdersFunction"},
                    },
                    {
                        "Name": "Resource",
                        "Value": {"Fn::Sub": "${OrdersFunction}:prod"},
                    }
                ],
                [
                    {
                        "Name": "FunctionName",
                        "Value": {"Ref": "OrdersFunction"},
                    },
                    {
                        "Name": "Resource",
                        "Value": {"Fn::Sub": "${OrdersFunction}:prod"},
                    }
                ],
            ],
            "direct version invokes must not contribute to the prod alias alarm",
        )

    def test_pinned_transport_keeps_the_invoke_helper_at_the_learner_path(self):
        self.assertTrue(INVOKE_LOOP.is_file(), "missing learner invoke-loop helper")
        template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        user_data = template["Resources"]["LabWorkstation"]["Properties"][
            "UserData"
        ]["Fn::Base64"]["Fn::Sub"]
        self.assertIn("assets/helpers/invoke-loop.py", user_data)
        self.assertIn("$LAB_ROOT/bin/invoke-loop", user_data)

    def test_workstation_downloads_helper_before_aws_credentials_are_ready(self):
        template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        user_data = template["Resources"]["LabWorkstation"]["Properties"][
            "UserData"
        ]["Fn::Base64"]["Fn::Sub"]

        credentials_ready = user_data.index(
            "aws sts get-caller-identity >/dev/null\n"
        )
        helper_stage = user_data.index("BOOTSTRAP_STAGE=asset-download")
        helper_install = user_data.index(
            'download_asset "$INVOKE_LOOP_ASSET"'
        )

        self.assertLess(helper_stage, helper_install)
        self.assertLess(helper_install, credentials_ready)
        self.assertNotIn("install -o cloud_user -g cloud_user -m755 /dev/stdin", user_data)

    def test_userdata_retains_three_kibibytes_of_ec2_limit_headroom(self):
        template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        user_data = template["Resources"]["LabWorkstation"]["Properties"][
            "UserData"
        ]["Fn::Base64"]["Fn::Sub"]

        self.assertLessEqual(
            len(user_data.encode("utf-8")),
            16_384 - 3_072,
            "workstation UserData must not sit on the EC2 raw-data limit",
        )


if __name__ == "__main__":
    unittest.main()
