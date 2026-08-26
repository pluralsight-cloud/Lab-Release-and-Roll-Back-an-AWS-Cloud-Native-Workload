import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RECORD_OUTCOME = REPOSITORY_ROOT / "assets/helpers/record-outcome.py"

ORIGINAL = {
    "deploymentInfo": {
        "deploymentId": "d-ORIGINAL",
        "creator": "user",
        "status": "Stopped",
        "rollbackInfo": {"rollbackDeploymentId": "d-ROLLBACK"},
    }
}
ROLLBACK = {
    "deploymentInfo": {
        "deploymentId": "d-ROLLBACK",
        "creator": "codeDeployRollback",
        "status": "Succeeded",
        "rollbackInfo": {"rollbackTriggeringDeploymentId": "d-ORIGINAL"},
    }
}
ALIAS = {
    "AliasArn": (
        "arn:aws:lambda:us-east-1:111122223333:"
        "function:globomantics-orders:prod"
    ),
    "FunctionVersion": "1",
    "Name": "prod",
    "RoutingConfig": {},
}
ALARM = {
    "MetricAlarms": [
        {"AlarmName": "globomantics-orders-errors", "StateValue": "OK"}
    ]
}
HISTORY = {
    "AlarmHistoryItems": [
        {
            "HistorySummary": "Alarm updated from ALARM to OK",
            "Timestamp": "2026-08-26T12:10:00Z",
            "HistoryItemType": "StateUpdate",
        }
    ]
}


def load_record_outcome_module():
    spec = importlib.util.spec_from_file_location("record_outcome", RECORD_OUTCOME)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Task6RecordOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temporary_root = Path(self.temporary_directory.name)
        self.fake_bin = self.temporary_root / "bin"
        self.fake_bin.mkdir()
        self.call_log = self.temporary_root / "aws-calls.jsonl"
        fake_aws = self.fake_bin / "aws"
        fake_aws.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys

arguments = sys.argv[1:]
with open(os.environ["FAKE_AWS_CALL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(arguments) + "\\n")

if arguments[:2] == ["deploy", "get-deployment"]:
    deployment_id = arguments[arguments.index("--deployment-id") + 1]
    response_key = "original" if deployment_id == "d-ORIGINAL" else "rollback"
elif arguments[:2] == ["lambda", "get-alias"]:
    response_key = "alias"
elif arguments[:2] == ["cloudwatch", "describe-alarms"]:
    response_key = "alarm"
elif arguments[:2] == ["cloudwatch", "describe-alarm-history"]:
    response_key = "history"
else:
    print("unexpected AWS CLI operation: " + " ".join(arguments), file=sys.stderr)
    raise SystemExit(97)

response = json.loads(os.environ["FAKE_AWS_RESPONSES"])[response_key]
if response.get("stderr"):
    print(response["stderr"], file=sys.stderr)
if "raw" in response:
    print(response["raw"])
else:
    print(json.dumps(response["json"]))
raise SystemExit(response.get("exit_code", 0))
""",
            encoding="utf-8",
        )
        fake_aws.chmod(0o755)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def valid_responses(self):
        return {
            "original": {"json": copy.deepcopy(ORIGINAL)},
            "rollback": {"json": copy.deepcopy(ROLLBACK)},
            "alias": {"json": copy.deepcopy(ALIAS)},
            "alarm": {"json": copy.deepcopy(ALARM)},
            "history": {"json": copy.deepcopy(HISTORY)},
        }

    def run_helper(self, responses, outcome_path):
        module = load_record_outcome_module()
        environment = {
            **os.environ,
            "PATH": f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_AWS_CALL_LOG": str(self.call_log),
            "FAKE_AWS_RESPONSES": json.dumps(responses),
        }
        stderr = io.StringIO()
        arguments = [
            str(RECORD_OUTCOME),
            "--original-id",
            "d-ORIGINAL",
            "--rollback-id",
            "d-ROLLBACK",
        ]
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(module, "OUTCOME_PATH", outcome_path),
            mock.patch.object(sys, "argv", arguments),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = module.main()
        return return_code, stderr.getvalue()

    def assert_rejected_without_output(self, responses):
        outcome_path = self.temporary_root / "release-outcome.txt"
        return_code, stderr = self.run_helper(responses, outcome_path)
        self.assertNotEqual(return_code, 0)
        self.assertTrue(stderr.strip(), "rejection needs a learner-readable diagnostic")
        self.assertFalse(outcome_path.exists(), "failed run created outcome evidence")

        sentinel = b"existing verified outcome\n"
        outcome_path.write_bytes(sentinel)
        return_code, stderr = self.run_helper(responses, outcome_path)
        self.assertNotEqual(return_code, 0)
        self.assertTrue(stderr.strip(), "rejection needs a learner-readable diagnostic")
        self.assertEqual(
            outcome_path.read_bytes(),
            sentinel,
            "failed run replaced existing outcome evidence",
        )

    def test_confirmed_recovery_writes_complete_outcome_after_authorized_reads(self):
        outcome_path = self.temporary_root / "release-outcome.txt"

        return_code, stderr = self.run_helper(self.valid_responses(), outcome_path)

        self.assertEqual(return_code, 0, stderr)
        self.assertEqual(stderr, "")
        outcome_bytes = outcome_path.read_bytes()
        self.assertTrue(outcome_bytes.endswith(b"\n"))
        outcome = json.loads(outcome_bytes)
        self.assertEqual(outcome["result"], "RECOVERED")
        self.assertRegex(
            outcome["captured_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$",
        )
        self.assertEqual(outcome["original_deployment"], ORIGINAL["deploymentInfo"])
        self.assertEqual(outcome["rollback_deployment"], ROLLBACK["deploymentInfo"])
        self.assertEqual(outcome["final_alias"], ALIAS)
        self.assertEqual(outcome["final_alarm"], ALARM["MetricAlarms"][0])
        self.assertEqual(outcome["alarm_history"], HISTORY["AlarmHistoryItems"])

        calls = [
            json.loads(line)
            for line in self.call_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            calls,
            [
                [
                    "deploy",
                    "get-deployment",
                    "--deployment-id",
                    "d-ORIGINAL",
                    "--output",
                    "json",
                    "--no-cli-pager",
                ],
                [
                    "deploy",
                    "get-deployment",
                    "--deployment-id",
                    "d-ROLLBACK",
                    "--output",
                    "json",
                    "--no-cli-pager",
                ],
                [
                    "lambda",
                    "get-alias",
                    "--function-name",
                    "globomantics-orders",
                    "--name",
                    "prod",
                    "--output",
                    "json",
                    "--no-cli-pager",
                ],
                [
                    "cloudwatch",
                    "describe-alarms",
                    "--alarm-names",
                    "globomantics-orders-errors",
                    "--output",
                    "json",
                    "--no-cli-pager",
                ],
                [
                    "cloudwatch",
                    "describe-alarm-history",
                    "--alarm-name",
                    "globomantics-orders-errors",
                    "--history-item-type",
                    "StateUpdate",
                    "--output",
                    "json",
                    "--no-cli-pager",
                ],
            ],
        )

    def test_identical_deployment_ids_are_rejected(self):
        module = load_record_outcome_module()
        outcome_path = self.temporary_root / "release-outcome.txt"
        environment = {
            **os.environ,
            "PATH": f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_AWS_CALL_LOG": str(self.call_log),
            "FAKE_AWS_RESPONSES": json.dumps(self.valid_responses()),
        }
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(module, "OUTCOME_PATH", outcome_path),
            mock.patch.object(
                sys,
                "argv",
                [
                    str(RECORD_OUTCOME),
                    "--original-id",
                    "d-ORIGINAL",
                    "--rollback-id",
                    "d-ORIGINAL",
                ],
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return_code = module.main()
        self.assertNotEqual(return_code, 0)
        self.assertFalse(outcome_path.exists())
        self.assertFalse(self.call_log.exists(), "identical IDs need no AWS reads")

        sentinel = b"existing verified outcome\n"
        outcome_path.write_bytes(sentinel)
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(module, "OUTCOME_PATH", outcome_path),
            mock.patch.object(
                sys,
                "argv",
                [
                    str(RECORD_OUTCOME),
                    "--original-id",
                    "d-ORIGINAL",
                    "--rollback-id",
                    "d-ORIGINAL",
                ],
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return_code = module.main()
        self.assertNotEqual(return_code, 0)
        self.assertEqual(outcome_path.read_bytes(), sentinel)

    def test_original_not_stopped_is_rejected_without_creating_or_replacing_output(self):
        responses = self.valid_responses()
        responses["original"]["json"]["deploymentInfo"]["status"] = "Failed"
        self.assert_rejected_without_output(responses)

    def test_original_pointing_to_another_rollback_is_rejected_without_output(self):
        responses = self.valid_responses()
        responses["original"]["json"]["deploymentInfo"]["rollbackInfo"] = {
            "rollbackDeploymentId": "d-OTHER"
        }
        self.assert_rejected_without_output(responses)

    def test_non_rollback_creator_is_rejected_without_creating_or_replacing_output(self):
        responses = self.valid_responses()
        responses["rollback"]["json"]["deploymentInfo"]["creator"] = "user"
        self.assert_rejected_without_output(responses)

    def test_unsucceeded_rollback_is_rejected_without_creating_or_replacing_output(self):
        responses = self.valid_responses()
        responses["rollback"]["json"]["deploymentInfo"]["status"] = "Failed"
        self.assert_rejected_without_output(responses)

    def test_rollback_pointing_to_another_original_is_rejected_without_output(self):
        responses = self.valid_responses()
        responses["rollback"]["json"]["deploymentInfo"]["rollbackInfo"] = {
            "rollbackTriggeringDeploymentId": "d-OTHER"
        }
        self.assert_rejected_without_output(responses)

    def test_alias_not_at_v1_is_rejected_without_creating_or_replacing_output(self):
        responses = self.valid_responses()
        responses["alias"]["json"]["FunctionVersion"] = "2"
        self.assert_rejected_without_output(responses)

    def test_alias_routing_weight_is_rejected_without_creating_or_replacing_output(self):
        responses = self.valid_responses()
        responses["alias"]["json"]["RoutingConfig"] = {
            "AdditionalVersionWeights": {"2": 0.1}
        }
        self.assert_rejected_without_output(responses)

    def test_alarm_not_ok_is_rejected_without_creating_or_replacing_output(self):
        responses = self.valid_responses()
        responses["alarm"]["json"]["MetricAlarms"][0]["StateValue"] = "ALARM"
        self.assert_rejected_without_output(responses)

    def test_wrong_alarm_name_is_rejected_without_creating_or_replacing_output(self):
        responses = self.valid_responses()
        responses["alarm"]["json"]["MetricAlarms"][0]["AlarmName"] = "other"
        self.assert_rejected_without_output(responses)

    def test_alarm_cardinality_is_rejected_without_creating_or_replacing_output(self):
        responses = self.valid_responses()
        responses["alarm"]["json"]["MetricAlarms"].append(
            copy.deepcopy(ALARM["MetricAlarms"][0])
        )
        self.assert_rejected_without_output(responses)

    def test_malformed_aws_json_is_rejected_without_creating_or_replacing_output(self):
        responses = self.valid_responses()
        responses["original"] = {"raw": "{not-json"}
        self.assert_rejected_without_output(responses)

    def test_nonzero_aws_cli_exit_is_rejected_without_creating_or_replacing_output(self):
        responses = self.valid_responses()
        responses["original"] = {
            "raw": "",
            "stderr": "simulated access denial",
            "exit_code": 42,
        }
        self.assert_rejected_without_output(responses)


if __name__ == "__main__":
    unittest.main()
