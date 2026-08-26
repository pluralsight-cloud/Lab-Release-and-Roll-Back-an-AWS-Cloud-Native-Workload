import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPOSITORY_ROOT / "infrastructure/template.yaml"

ALARM_METRICS = [
    {
        "Id": "errors",
        "MetricStat": {
            "Metric": {
                "Dimensions": [
                    {"Name": "Resource", "Value": "globomantics-orders:prod"}
                ],
                "MetricName": "Errors",
                "Namespace": "AWS/Lambda",
            },
            "Period": 60,
            "Stat": "Sum",
        },
        "ReturnData": False,
    },
    {
        "Id": "invocations",
        "MetricStat": {
            "Metric": {
                "Dimensions": [
                    {"Name": "Resource", "Value": "globomantics-orders:prod"}
                ],
                "MetricName": "Invocations",
                "Namespace": "AWS/Lambda",
            },
            "Period": 60,
            "Stat": "Sum",
        },
        "ReturnData": False,
    },
    {
        "Expression": "IF(invocations>0,FILL(errors,0))",
        "Id": "health",
        "ReturnData": True,
    },
]
ALARM_TEMPLATE_METRICS = [
    {
        "Id": "errors",
        "MetricStat": {
            "Metric": {
                "Dimensions": [
                    {
                        "Name": "Resource",
                        "Value": {"Fn::Sub": "${OrdersFunction}:prod"},
                    }
                ],
                "MetricName": "Errors",
                "Namespace": "AWS/Lambda",
            },
            "Period": 60,
            "Stat": "Sum",
        },
        "ReturnData": False,
    },
    {
        "Id": "invocations",
        "MetricStat": {
            "Metric": {
                "Dimensions": [
                    {
                        "Name": "Resource",
                        "Value": {"Fn::Sub": "${OrdersFunction}:prod"},
                    }
                ],
                "MetricName": "Invocations",
                "Namespace": "AWS/Lambda",
            },
            "Period": 60,
            "Stat": "Sum",
        },
        "ReturnData": False,
    },
    {
        "Expression": "IF(invocations>0,FILL(errors,0))",
        "Id": "health",
        "ReturnData": True,
    },
]

READY_ALARM = {
    "AlarmName": "globomantics-orders-errors",
    "ComparisonOperator": "GreaterThanThreshold",
    "DatapointsToAlarm": 1,
    "EvaluationPeriods": 1,
    "Metrics": ALARM_METRICS,
    "StateValue": "OK",
    "Threshold": 0.0,
    "TreatMissingData": "notBreaching",
}
HEALTHY_EXPRESSION_DATA = {
    "MetricDataResults": [
        {
            "Id": "health",
            "Label": "Alias healthy traffic errors",
            "StatusCode": "Complete",
            "Timestamps": ["2026-08-26T12:01:00Z"],
            "Values": [0.0],
        }
    ]
}
NO_TRAFFIC_EXPRESSION_DATA = {
    "MetricDataResults": [
        {
            "Id": "health",
            "Label": "Alias healthy traffic errors",
            "StatusCode": "Complete",
            "Timestamps": [],
            "Values": [],
        }
    ]
}
ERROR_EXPRESSION_DATA = {
    "MetricDataResults": [
        {
            "Id": "health",
            "Label": "Alias healthy traffic errors",
            "StatusCode": "Complete",
            "Timestamps": ["2026-08-26T12:01:00Z"],
            "Values": [1.0],
        }
    ]
}


def user_data():
    template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    return template["Resources"]["LabWorkstation"]["Properties"]["UserData"][
        "Fn::Base64"
    ]["Fn::Sub"]


def extract_shell_function(script, function_name):
    match = re.search(
        rf"(?ms)^{re.escape(function_name)}\(\) \{{\n.*?^\}}$",
        script,
    )
    if match is None:
        raise AssertionError(f"missing {function_name} shell function")
    return match.group(0)


class Task4AlarmPrewarmTests(unittest.TestCase):
    def run_prewarm(self, alarm, metric_data):
        prewarm_alarm = extract_shell_function(user_data(), "prewarm_alarm")

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            lab_root = temporary_root / "lab"
            state_directory = lab_root / "state"
            state_directory.mkdir(parents=True)
            fake_bin = temporary_root / "bin"
            fake_bin.mkdir()

            fixtures_path = temporary_root / "fixtures.json"
            fixtures_path.write_text(
                json.dumps(
                    {
                        "alarm": alarm,
                        "metric_data": metric_data,
                        "metric_queries": ALARM_METRICS,
                    }
                ),
                encoding="utf-8",
            )
            calls_path = temporary_root / "calls.jsonl"

            fake_aws = fake_bin / "aws"
            fake_aws.write_text(
                r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_AWS_CALLS"], "a", encoding="utf-8") as calls:
    calls.write(json.dumps(args) + "\n")
fixtures = json.load(open(os.environ["FAKE_AWS_FIXTURES"], encoding="utf-8"))

def require_pair(name, value):
    try:
        index = args.index(name)
    except ValueError:
        raise SystemExit(f"missing {name}")
    if index + 1 >= len(args) or args[index + 1] != value:
        raise SystemExit(f"unexpected {name}: {args[index + 1:index + 2]}")

if args[:2] == ["lambda", "invoke"]:
    require_pair("--function-name", "globomantics-orders")
    require_pair("--qualifier", "prod")
    payload_path = args[-1]
    open(payload_path, "w", encoding="utf-8").write(
        '{"order_id":"order-1001","status":"confirmed","version":"v1"}'
    )
    print('{"StatusCode":200,"ExecutedVersion":"1"}')
elif args[:2] == ["cloudwatch", "describe-alarms"]:
    require_pair("--alarm-names", "globomantics-orders-errors")
    print(json.dumps(fixtures["alarm"]))
elif args[:2] == ["cloudwatch", "get-metric-data"]:
    try:
        query_path = args[args.index("--metric-data-queries") + 1]
    except (ValueError, IndexError):
        raise SystemExit("missing metric data queries")
    if not query_path.startswith("file://"):
        raise SystemExit("metric data queries must use a file")
    if json.load(open(query_path.removeprefix("file://"), encoding="utf-8")) != fixtures["metric_queries"]:
        raise SystemExit("unexpected metric math query")
    require_pair("--output", "json")
    if "--start-time" not in args or "--end-time" not in args:
        raise SystemExit("metric request must use a bounded time window")
    print(json.dumps(fixtures["metric_data"]))
elif args[:2] == ["cloudwatch", "describe-alarm-history"]:
    raise SystemExit("describe-alarm-history is forbidden")
else:
    raise SystemExit(f"unexpected fake AWS call: {' '.join(args)}")
''',
                encoding="utf-8",
            )
            fake_aws.chmod(0o755)

            fake_sleep = fake_bin / "sleep"
            fake_sleep.write_text(
                """#!/bin/bash
if [ "$#" -ne 1 ] || [ "$1" != "$ALARM_POLL_SECONDS" ]; then
  printf '%s\n' "unexpected sleep: $*" >&2
  exit 65
fi
""",
                encoding="utf-8",
            )
            fake_sleep.chmod(0o755)

            script = f'''set -euo pipefail
chown() {{ :; }}
{prewarm_alarm}
LAB_ROOT={shlex.quote(str(lab_root))}
FUNCTION_NAME=globomantics-orders
ALARM_NAME=globomantics-orders-errors
ALARM_MAX_ATTEMPTS=2
ALARM_POLL_SECONDS=5
export ALARM_POLL_SECONDS
prewarm_alarm
'''
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "FAKE_AWS_FIXTURES": str(fixtures_path),
                "FAKE_AWS_CALLS": str(calls_path),
            }
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env=environment,
            )
            state = {
                path.name: json.loads(path.read_text(encoding="utf-8"))
                for path in state_directory.glob("alarm-*.json")
            }
            calls = [
                json.loads(line)
                for line in calls_path.read_text(encoding="utf-8").splitlines()
            ]
            return result, state, calls

    def test_alarm_uses_alias_traffic_metric_math_contract(self):
        alarm = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))["Resources"][
            "OrdersErrorsAlarm"
        ]["Properties"]

        self.assertEqual(alarm["Metrics"], ALARM_TEMPLATE_METRICS)
        self.assertEqual(alarm["TreatMissingData"], "notBreaching")
        for removed_property in (
            "Dimensions",
            "MetricName",
            "Namespace",
            "Period",
            "Statistic",
        ):
            self.assertNotIn(removed_property, alarm)

    def test_prewarm_accepts_successful_alias_traffic_expression_at_zero(self):
        result, state, calls = self.run_prewarm(READY_ALARM, HEALTHY_EXPRESSION_DATA)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            state,
            {
                "alarm-before.json": READY_ALARM,
                "alarm-ready.json": READY_ALARM,
                "alarm-zero-datapoints.json": HEALTHY_EXPRESSION_DATA,
            },
        )
        self.assertTrue(
            any(call[:2] == ["cloudwatch", "get-metric-data"] for call in calls)
        )

    def test_prewarm_rejects_missing_expression_value_even_when_sparse_alarm_is_ok(self):
        result, state, _ = self.run_prewarm(
            READY_ALARM,
            NO_TRAFFIC_EXPRESSION_DATA,
        )

        self.assertEqual(READY_ALARM["StateValue"], "OK")
        self.assertEqual(READY_ALARM["TreatMissingData"], "notBreaching")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("alarm-zero-datapoints.json", state)
        self.assertIn(
            "Orders errors alarm did not reach OK with a real zero-error datapoint.",
            result.stderr,
        )

    def test_prewarm_rejects_error_expression_value(self):
        result, state, _ = self.run_prewarm(READY_ALARM, ERROR_EXPRESSION_DATA)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("alarm-zero-datapoints.json", state)
        self.assertIn(
            "Orders errors alarm did not reach OK with a real zero-error datapoint.",
            result.stderr,
        )

    def test_bootstrap_starts_alarm_before_lambda_publish_and_joins_before_policy_delete(
        self,
    ):
        template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        workstation = template["Resources"]["LabWorkstation"]
        script = user_data()

        self.assertIn("OrdersErrorsAlarm", workstation["DependsOn"])
        credentials_ready = script.index("aws sts get-caller-identity >/dev/null\n")
        alarm_start = script.index("prewarm_alarm &")
        alarm_pid = script.index("ALARM_PID=$!")
        helper_install = script.index("BOOTSTRAP_STAGE=invoke-helper-install")
        lambda_publish = script.index("BOOTSTRAP_STAGE=lambda-publish")
        alarm_join = script.index('if ! wait "$ALARM_PID"; then')
        policy_delete = script.index("BOOTSTRAP_STAGE=iam-policy-delete")
        success_signal = script.index("signal_bootstrap SUCCESS")

        self.assertLess(credentials_ready, alarm_start)
        self.assertLess(alarm_start, alarm_pid)
        self.assertLess(alarm_pid, helper_install)
        self.assertLess(alarm_start, lambda_publish)
        self.assertLess(lambda_publish, alarm_join)
        self.assertLess(alarm_join, policy_delete)
        self.assertLess(policy_delete, success_signal)

    def test_bootstrap_propagates_background_alarm_failure_to_failure_signal(self):
        script = user_data()
        wait_block = re.search(
            r'''(?ms)^BOOTSTRAP_STAGE=alarm-prewarm\n'''
            r'''if ! wait "\$ALARM_PID"; then\n'''
            r'''\s*signal_bootstrap FAILURE\n'''
            r'''\s*exit 1\n'''
            r'''\s*fi$''',
            script,
        )

        self.assertIsNotNone(wait_block, "background alarm failure must fail bootstrap")
        join_script = f'''set -euo pipefail
signal_bootstrap() {{ printf 'signal:%s:stage:%s\n' "$1" "$BOOTSTRAP_STAGE"; }}
(exit 7) &
ALARM_PID=$!
{wait_block.group(0)}
printf 'continued-after-failure\n'
'''
        result = subprocess.run(
            ["bash", "-c", join_script],
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "signal:FAILURE:stage:alarm-prewarm\n")
        self.assertEqual(script.count("signal_bootstrap SUCCESS"), 1)
        self.assertIn("trap 'signal_bootstrap FAILURE' ERR", script)

    def test_creation_policy_covers_concurrent_bounded_waits_with_api_headroom(self):
        template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        workstation = template["Resources"]["LabWorkstation"]
        script = user_data()

        def shell_integer(name):
            match = re.search(rf"(?m)^\s*{re.escape(name)}=(\d+)$", script)
            self.assertIsNotNone(match, f"missing {name} shell setting")
            return int(match.group(1))

        timeout = workstation["CreationPolicy"]["ResourceSignal"]["Timeout"]
        timeout_match = re.fullmatch(r"PT(\d+)M", timeout)
        self.assertIsNotNone(timeout_match, "readiness timeout must be whole minutes")
        timeout_seconds = int(timeout_match.group(1)) * 60

        credentials_wait = 29 * 2
        lambda_wait = 29 * 2
        alarm_wait = (
            shell_integer("ALARM_MAX_ATTEMPTS") - 1
        ) * shell_integer("ALARM_POLL_SECONDS")
        maximum_intentional_wait = credentials_wait + max(lambda_wait, alarm_wait)

        self.assertGreaterEqual(
            timeout_seconds,
            maximum_intentional_wait + 120,
            "CreationPolicy needs two minutes of API/runtime headroom beyond concurrent sleeps",
        )

    def test_prewarm_uses_only_bounded_poll_sleep_and_required_apis(self):
        prewarm_alarm = extract_shell_function(user_data(), "prewarm_alarm")

        self.assertEqual(
            re.findall(r"(?m)^\s*sleep (.+)$", prewarm_alarm),
            ['"$ALARM_POLL_SECONDS"'],
        )
        self.assertEqual(
            set(re.findall(r"aws (lambda invoke|cloudwatch [a-z-]+)", prewarm_alarm)),
            {
                "lambda invoke",
                "cloudwatch describe-alarms",
                "cloudwatch get-metric-data",
            },
        )
        self.assertNotIn("StateReasonData", prewarm_alarm)


if __name__ == "__main__":
    unittest.main()
