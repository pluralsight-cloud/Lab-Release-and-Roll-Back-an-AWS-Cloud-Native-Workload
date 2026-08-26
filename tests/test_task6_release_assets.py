import json
import re
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APPSPEC = REPOSITORY_ROOT / "assets/appspec/release-v2.json"
RECORD_OUTCOME = REPOSITORY_ROOT / "assets/helpers/record-outcome.py"
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

    def test_pinned_transport_keeps_the_appspec_source_identity(self):
        self.assertTrue(APPSPEC.is_file(), "missing learner release AppSpec source")
        self.assertIn("assets/appspec/release-v2.json", user_data())

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
            'download_asset "$APPSPEC_ASSET" "$APPSPEC_SHA256" ' + appspec_path,
            script,
        )

    def test_pinned_transport_keeps_the_outcome_helper_at_its_learner_path(self):
        self.assertTrue(RECORD_OUTCOME.is_file(), "missing recovery outcome helper")
        script = user_data()
        helper_path = '"$LAB_ROOT/bin/record-outcome"'
        self.assertIn(
            'download_asset "$RECORD_OUTCOME_ASSET" "$RECORD_OUTCOME_SHA256" '
            + helper_path,
            script,
        )

    def test_permanent_policy_preserves_exact_learner_access_without_bootstrap_writes(self):
        template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        policies = template["Resources"]["LabWorkstationRole"]["Properties"][
            "Policies"
        ]
        permanent = next(
            policy
            for policy in policies
            if policy["PolicyName"] == "globomantics-orders-workstation-access"
        )
        statements = permanent["PolicyDocument"]["Statement"]
        actions = {
            action
            for statement in statements
            for action in (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
        }

        self.assertEqual(
            actions,
            {
                "lambda:GetAlias",
                "lambda:GetFunctionConfiguration",
                "lambda:InvokeFunction",
                "lambda:ListVersionsByFunction",
                "codedeploy:CreateDeployment",
                "codedeploy:GetApplication",
                "codedeploy:GetDeployment",
                "codedeploy:GetDeploymentConfig",
                "codedeploy:GetDeploymentGroup",
                "codedeploy:ListDeployments",
                "codedeploy:RegisterApplicationRevision",
                "codedeploy:StopDeployment",
                "cloudwatch:DescribeAlarmHistory",
                "cloudwatch:DescribeAlarms",
                "cloudformation:SignalResource",
            },
        )
        self.assertTrue(
            {
                "lambda:UpdateFunctionCode",
                "cloudwatch:GetMetricStatistics",
                "iam:DeleteRolePolicy",
            }.isdisjoint(actions),
            "bootstrap-only writes must not survive in the permanent policy",
        )

    def test_deployment_group_still_has_no_alarm_wiring(self):
        template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        properties = template["Resources"]["OrdersDeploymentGroup"]["Properties"]

        self.assertNotIn("AlarmConfiguration", properties)
        self.assertNotIn(
            "DEPLOYMENT_STOP_ON_ALARM",
            properties["AutoRollbackConfiguration"]["Events"],
        )


if __name__ == "__main__":
    unittest.main()
