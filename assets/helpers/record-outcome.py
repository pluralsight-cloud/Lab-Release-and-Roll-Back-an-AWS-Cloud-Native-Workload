#!/usr/bin/env python3
"""Record evidence only after the expected CodeDeploy recovery."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

FUNCTION = "globomantics-orders"
ALIAS = "prod"
ALARM = "globomantics-orders-errors"
OUTCOME_PATH = Path("/home/cloud_user/lab/output/release-outcome.txt")


def reject(message):
    raise ValueError(message)


def field(value, *path):
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def aws_json(*arguments: str) -> dict:
    result = subprocess.run(
        ["aws", *arguments, "--output", "json", "--no-cli-pager"],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        reject(result.stderr.strip() or f"AWS CLI exit {result.returncode}")
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        reject("AWS CLI returned malformed JSON")
    if not isinstance(response, dict):
        reject("AWS CLI response is not an object")
    return response


def validate_recovery(
    original_id: str,
    rollback_id: str,
    original: dict,
    rollback: dict,
    alias: dict,
    alarm: dict,
) -> None:
    if original_id == rollback_id:
        reject("deployment IDs are identical")
    checks = (
        (field(original, "deploymentInfo", "deploymentId"), original_id),
        (field(original, "deploymentInfo", "status"), "Stopped"),
        (
            field(original, "deploymentInfo", "rollbackInfo", "rollbackDeploymentId"),
            rollback_id,
        ),
        (field(rollback, "deploymentInfo", "deploymentId"), rollback_id),
        (field(rollback, "deploymentInfo", "creator"), "codeDeployRollback"),
        (field(rollback, "deploymentInfo", "status"), "Succeeded"),
        (
            field(
                rollback,
                "deploymentInfo",
                "rollbackInfo",
                "rollbackTriggeringDeploymentId",
            ),
            original_id,
        ),
        (field(alias, "FunctionVersion"), "1"),
    )
    if not all(actual == expected for actual, expected in checks):
        reject("deployment or alias recovery state is unexpected")
    if field(alias, "RoutingConfig") not in (None, {}):
        reject("alias has routing weight")
    alarms = field(alarm, "MetricAlarms")
    if not isinstance(alarms, list) or len(alarms) != 1:
        reject("expected one alarm")
    if field(alarms[0], "AlarmName") != ALARM or field(alarms[0], "StateValue") != "OK":
        reject("alarm identity or state is unexpected")


def build_outcome(
    original_id: str,
    rollback_id: str,
    original: dict,
    rollback: dict,
    alias: dict,
    alarm: dict,
    history: dict,
) -> dict:
    items = field(history, "AlarmHistoryItems")
    if not isinstance(items, list):
        reject("alarm history is malformed")
    return {
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "result": "RECOVERED",
        "original_deployment": field(original, "deploymentInfo"),
        "rollback_deployment": field(rollback, "deploymentInfo"),
        "final_alias": alias,
        "final_alarm": field(alarm, "MetricAlarms")[0],
        "alarm_history": items,
    }


def write_atomically(path: Path, outcome: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(outcome, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Record the completed recovery.")
    parser.add_argument("--original-id", required=True)
    parser.add_argument("--rollback-id", required=True)
    args = parser.parse_args()
    original_id, rollback_id = args.original_id, args.rollback_id
    try:
        if original_id == rollback_id:
            reject("deployment IDs are identical")
        original, rollback, alias, alarm, history = [
            aws_json(*query)
            for query in (
                ("deploy", "get-deployment", "--deployment-id", original_id),
                ("deploy", "get-deployment", "--deployment-id", rollback_id),
                ("lambda", "get-alias", "--function-name", FUNCTION, "--name", ALIAS),
                ("cloudwatch", "describe-alarms", "--alarm-names", ALARM),
                (
                    "cloudwatch",
                    "describe-alarm-history",
                    "--alarm-name",
                    ALARM,
                    "--history-item-type",
                    "StateUpdate",
                ),
            )
        ]
        validate_recovery(original_id, rollback_id, original, rollback, alias, alarm)
        write_atomically(
            OUTCOME_PATH,
            build_outcome(
                original_id, rollback_id, original, rollback, alias, alarm, history
            ),
        )
    except (OSError, ValueError) as error:
        print(f"Recovery not confirmed: {error}", file=sys.stderr)
        return 1
    print(f"Recovery confirmed. Outcome written to {OUTCOME_PATH}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
