#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VALIDATION_TMP=$(mktemp -d)
trap 'rm -rf "$VALIDATION_TMP"' EXIT

cd "$REPOSITORY_ROOT"
export PYTHONDONTWRITEBYTECODE=1

VALIDATION_INNER=1 python3 -m unittest discover -s tests -p 'test_*.py' -v
PYTHONPYCACHEPREFIX="$VALIDATION_TMP/pycache" python3 -m py_compile \
  assets/function/v2/index.py \
  assets/helpers/invoke-loop.py \
  assets/helpers/record-outcome.py \
  scripts/package-v2.py
python3 scripts/package-v2.py --output "$VALIDATION_TMP/v2.zip"
cmp assets/function/v2.zip "$VALIDATION_TMP/v2.zip"
python3 -c 'import yaml; yaml.safe_load(open("infrastructure/template.yaml", encoding="utf-8"))'
cfn-lint --ignore-checks E1152 -- infrastructure/template.yaml
git diff --check

python3 - "$VALIDATION_TMP" <<'PY'
import hashlib
import re
import subprocess
import sys
from pathlib import Path

import yaml


temporary_directory = Path(sys.argv[1])
repository_root = Path.cwd()
template_path = repository_root / "infrastructure/template.yaml"
template_bytes = template_path.read_bytes()
if len(template_bytes) > 51_200:
    raise SystemExit("template body exceeds 51,200 bytes")

template = yaml.safe_load(template_bytes)
try:
    user_data = template["Resources"]["LabWorkstation"]["Properties"]["UserData"][
        "Fn::Base64"
    ]["Fn::Sub"]
except (KeyError, TypeError) as error:
    raise SystemExit("LabWorkstation UserData must be Fn::Base64/Fn::Sub") from error

if not isinstance(user_data, str):
    raise SystemExit("LabWorkstation UserData must be a string")
if len(user_data.encode("utf-8")) > 13_312:
    raise SystemExit("raw UserData exceeds 13,312 bytes")

assignments = dict(
    re.findall(r"(?m)^([A-Z][A-Z0-9_]*)=([^\n]+)$", user_data)
)
lab_root = assignments.get("LAB_ROOT")
if lab_root != "/home/cloud_user/lab":
    raise SystemExit("required learner root is missing or changed")
assets = (
    ("V2", "assets/function/v2.zip", "V2_ASSET", "V2_SHA256", "$V2_ARCHIVE", "0644"),
    ("APPSPEC", "assets/appspec/release-v2.json", "APPSPEC_ASSET", "APPSPEC_SHA256", "$LAB_ROOT/appspec/release-v2.json", "0644"),
    ("INVOKE_LOOP", "assets/helpers/invoke-loop.py", "INVOKE_LOOP_ASSET", "INVOKE_LOOP_SHA256", "$LAB_ROOT/bin/invoke-loop", "0755"),
    ("RECORD_OUTCOME", "assets/helpers/record-outcome.py", "RECORD_OUTCOME_ASSET", "RECORD_OUTCOME_SHA256", "$LAB_ROOT/bin/record-outcome", "0755"),
)
if assignments.get("V2_ARCHIVE") != "$LAB_ROOT/assets/function/v2.zip":
    raise SystemExit("pinned transport mismatch: V2 destination")
for name, asset_path, asset_variable, sha_variable, destination, mode in assets:
    if assignments.get(asset_variable) != asset_path:
        raise SystemExit(f"pinned transport mismatch: {name} source path")
    actual_sha = hashlib.sha256((repository_root / asset_path).read_bytes()).hexdigest()
    if assignments.get(sha_variable) != actual_sha:
        raise SystemExit(f"pinned asset mismatch: {asset_path}")
    expected_call = f'download_asset "${asset_variable}" "${sha_variable}" "{destination}" {mode}'
    if expected_call not in user_data:
        raise SystemExit(f"pinned transport mismatch: {name}")

expected_download_calls = [
    (f"${asset_variable}", f"${sha_variable}", destination, mode)
    for _, _, asset_variable, sha_variable, destination, mode in assets
]
actual_download_calls = []
for line in user_data.splitlines():
    stripped_line = line.strip()
    if not re.match(r"download_asset(?:\s|$)", stripped_line):
        continue
    parsed_call = re.fullmatch(
        r'download_asset\s+"([^\"]+)"\s+"([^\"]+)"\s+"([^\"]+)"\s+(\d+)',
        stripped_line,
    )
    if parsed_call is None:
        raise SystemExit("pinned transport mismatch: malformed download call")
    actual_download_calls.append(parsed_call.groups())
if actual_download_calls != expected_download_calls:
    raise SystemExit("pinned transport mismatch: ordered download calls")
if (
    len(re.findall(r"(?m)^\s*download_asset\(\)\s*\{", user_data)) != 1
    or len(re.findall(r"\bdownload_asset\b", user_data)) != 5
):
    raise SystemExit("pinned transport mismatch: download_asset occurrences")

asset_base_url = assignments.get("ASSET_BASE_URL", "")
expected_asset_base_url = (
    "https://raw.githubusercontent.com/pluralsight-cloud/"
    "Lab-Release-and-Roll-Back-an-AWS-Cloud-Native-Workload/"
    "b49e821a9871debed6cc1a7d98df0513f06a2199"
)
if asset_base_url != expected_asset_base_url:
    raise SystemExit("pinned GitHub raw URL must match the canonical immutable commit")
if '"$ASSET_BASE_URL/$asset_name"' not in user_data:
    raise SystemExit("pinned GitHub raw URL is not used by download_asset")
for required_install_step in (
    'chown cloud_user:cloud_user "$temporary_file"',
    'chmod "$file_mode" "$temporary_file"',
    'mv -f "$temporary_file" "$destination"',
):
    if required_install_step not in user_data:
        raise SystemExit("pinned transport install ownership or mode is missing")

for embedded_asset in (
    "INVOKE_LOOP_GZIP_BASE64",
    "RECORD_OUTCOME_GZIP_BASE64",
    "APPSPEC_GZIP_BASE64",
    "V2_ZIP_BASE64",
):
    if embedded_asset in user_data:
        raise SystemExit(f"embedded asset transport is forbidden: {embedded_asset}")

substitutions = {
    "AWS::StackName": "validation-stack",
    "AWS::Region": "us-east-1",
}
unknown_tokens = []


def render_substitution(match):
    token = match.group(1)
    if token.startswith("!"):
        return "${" + token[1:] + "}"
    replacement = substitutions.get(token)
    if replacement is None:
        unknown_tokens.append(token)
        return match.group(0)
    return replacement


rendered_user_data = re.sub(r"\$\{([^}]+)\}", render_substitution, user_data)
if unknown_tokens:
    raise SystemExit(
        "unknown Fn::Sub tokens in UserData: " + ", ".join(sorted(set(unknown_tokens)))
    )
rendered_user_data_path = temporary_directory / "user-data.sh"
rendered_user_data_path.write_text(rendered_user_data, encoding="utf-8")
subprocess.run(["bash", "-n", str(rendered_user_data_path)], check=True)

required_resources = {
    "LabVpc",
    "LabInternetGateway",
    "LabInternetGatewayAttachment",
    "LabPublicSubnet",
    "LabPublicRouteTable",
    "LabDefaultPublicRoute",
    "LabPublicSubnetRouteTableAssociation",
    "LabWorkstationSecurityGroup",
    "OrdersLambdaExecutionRole",
    "OrdersCodeDeployServiceRole",
    "LabWorkstationRole",
    "LabWorkstationInstanceProfile",
    "OrdersFunction",
    "OrdersV1Version",
    "OrdersProdAlias",
    "OrdersCodeDeployApplication",
    "OrdersDeploymentGroup",
    "OrdersErrorsAlarm",
    "LabWorkstation",
}
missing_resources = sorted(required_resources - set(template.get("Resources", {})))
if missing_resources:
    raise SystemExit("required resources missing: " + ", ".join(missing_resources))

required_outputs = {
    "pubIpAddress",
    "OrdersFunctionName",
    "OrdersProdAliasName",
    "OrdersV1Version",
    "OrdersV2Version",
    "OrdersCodeDeployApplicationName",
    "OrdersDeploymentGroupName",
    "OrdersErrorsAlarmName",
    "OrdersCodeDeployServiceRoleArn",
    "LabWorkstationId",
    "LabWorkstationRoleArn",
    "LabWorkstationPrivateIp",
}
missing_outputs = sorted(required_outputs - set(template.get("Outputs", {})))
if missing_outputs:
    raise SystemExit("required outputs missing: " + ", ".join(missing_outputs))


def action_set(statement):
    actions = statement["Action"]
    values = [actions] if isinstance(actions, str) else actions
    return {action.lower() for action in values}


def statement_map(policy):
    return {statement["Sid"]: statement for statement in policy["PolicyDocument"]["Statement"]}


roles = template["Resources"]
for logical_id, resource in roles.items():
    if resource.get("Type") != "AWS::IAM::Role":
        continue
    for policy in resource.get("Properties", {}).get("Policies", []):
        for statement in policy["PolicyDocument"].get("Statement", []):
            if "iam:passrole" in action_set(statement):
                raise SystemExit("IAM boundary violation: iam:PassRole is forbidden")

workstation_policies = {
    policy["PolicyName"]: policy
    for policy in roles["LabWorkstationRole"]["Properties"]["Policies"]
}
if set(workstation_policies) != {
    "globomantics-orders-workstation-access",
    "globomantics-orders-v2-bootstrap",
}:
    raise SystemExit("IAM boundary violation: unexpected workstation role policy")

permanent_statements = statement_map(
    workstation_policies["globomantics-orders-workstation-access"]
)
expected_permanent_actions = {
    "InspectAndInvokeOrdersWorkload": {
        "lambda:GetAlias",
        "lambda:GetFunctionConfiguration",
        "lambda:InvokeFunction",
        "lambda:ListVersionsByFunction",
    },
    "OperateOrdersDeployment": {
        "codedeploy:CreateDeployment",
        "codedeploy:GetApplication",
        "codedeploy:GetDeployment",
        "codedeploy:GetDeploymentConfig",
        "codedeploy:GetDeploymentGroup",
        "codedeploy:ListDeployments",
        "codedeploy:RegisterApplicationRevision",
    },
    "StopOrdersDeployment": {"codedeploy:StopDeployment"},
    "InspectOrdersAlarm": {"cloudwatch:DescribeAlarmHistory", "cloudwatch:DescribeAlarms"},
    "SignalWorkstationReadiness": {"cloudformation:SignalResource"},
}
expected_permanent_actions = {
    sid: {action.lower() for action in actions}
    for sid, actions in expected_permanent_actions.items()
}
if set(permanent_statements) != set(expected_permanent_actions) or any(
    action_set(permanent_statements[sid]) != actions
    for sid, actions in expected_permanent_actions.items()
):
    raise SystemExit("IAM boundary violation: permanent learner actions are not least-scope")

expected_permanent_resources = {
    "InspectAndInvokeOrdersWorkload": [
        {"Fn::Sub": "arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:function:globomantics-orders"},
        {"Fn::Sub": "arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:function:globomantics-orders:*"},
    ],
    "OperateOrdersDeployment": [
        {"Fn::Sub": "arn:${AWS::Partition}:codedeploy:${AWS::Region}:${AWS::AccountId}:application:globomantics-orders-app"},
        {"Fn::Sub": "arn:${AWS::Partition}:codedeploy:${AWS::Region}:${AWS::AccountId}:deploymentgroup:globomantics-orders-app/globomantics-orders-dg"},
        {"Fn::Sub": "arn:${AWS::Partition}:codedeploy:${AWS::Region}:${AWS::AccountId}:deploymentconfig:CodeDeployDefault.LambdaCanary10Percent5Minutes"},
    ],
    "StopOrdersDeployment": {
        "Fn::Sub": "arn:${AWS::Partition}:codedeploy:${AWS::Region}:${AWS::AccountId}:deploymentgroup:globomantics-orders-app/globomantics-orders-dg"
    },
    "InspectOrdersAlarm": "*",
    "SignalWorkstationReadiness": {
        "Fn::Sub": "arn:${AWS::Partition}:cloudformation:${AWS::Region}:${AWS::AccountId}:stack/${AWS::StackName}/*"
    },
}
if any(
    permanent_statements[sid].get("Resource") != resource
    for sid, resource in expected_permanent_resources.items()
):
    raise SystemExit("IAM boundary violation: permanent learner resources are not least-scope")

bootstrap_statements = statement_map(workstation_policies["globomantics-orders-v2-bootstrap"])
expected_bootstrap_actions = {
    "PublishOrdersV2DuringBootstrap": {"lambda:GetFunction", "lambda:UpdateFunctionCode"},
    "VerifyOrdersAlarmDatapointDuringBootstrap": {"cloudwatch:GetMetricData"},
    "RemoveOrdersV2BootstrapPermission": {"iam:DeleteRolePolicy"},
}
expected_bootstrap_actions = {
    sid: {action.lower() for action in actions}
    for sid, actions in expected_bootstrap_actions.items()
}
if set(bootstrap_statements) != set(expected_bootstrap_actions) or any(
    action_set(bootstrap_statements[sid]) != actions
    for sid, actions in expected_bootstrap_actions.items()
):
    raise SystemExit("IAM boundary violation: bootstrap actions are not temporary and least-scope")
if (
    bootstrap_statements["PublishOrdersV2DuringBootstrap"]["Resource"]
    != {"Fn::Sub": "arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:function:globomantics-orders"}
    or bootstrap_statements["VerifyOrdersAlarmDatapointDuringBootstrap"]["Resource"] != "*"
    or bootstrap_statements["RemoveOrdersV2BootstrapPermission"]["Resource"]
    != {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/globomantics-orders-workstation-role"}
):
    raise SystemExit("IAM boundary violation: bootstrap resources are not least-scope")
if (
    assignments.get("ROLE_NAME") != "globomantics-orders-workstation-role"
    or assignments.get("BOOTSTRAP_POLICY_NAME") != "globomantics-orders-v2-bootstrap"
    or 'aws iam delete-role-policy' not in user_data
    or '--role-name "$ROLE_NAME"' not in user_data
    or '--policy-name "$BOOTSTRAP_POLICY_NAME"' not in user_data
    or user_data.index('if ! wait "$ALARM_PID";')
    > user_data.index('aws iam delete-role-policy')
):
    raise SystemExit("IAM boundary violation: bootstrap policy removal is not after readiness join")

deployment_group = template["Resources"]["OrdersDeploymentGroup"]["Properties"]
if "AlarmConfiguration" in deployment_group or "DEPLOYMENT_STOP_ON_ALARM" in deployment_group[
    "AutoRollbackConfiguration"
]["Events"]:
    raise SystemExit("CodeDeploy alarm wiring is forbidden")

expected_alarm = {
    "AlarmDescription": "Alarm when real prod-alias traffic records an error.",
    "AlarmName": "globomantics-orders-errors",
    "ComparisonOperator": "GreaterThanThreshold",
    "DatapointsToAlarm": 1,
    "EvaluationPeriods": 1,
    "Metrics": [
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
    ],
    "Threshold": 0,
    "TreatMissingData": "breaching",
}
if template["Resources"]["OrdersErrorsAlarm"]["Properties"] != expected_alarm:
    raise SystemExit("alarm readiness contract is unexpected")

placeholder = "TARGET_VERSION"
allowed_placeholder_file = Path("assets/appspec/release-v2.json")
for source_root in (repository_root / "assets", repository_root / "infrastructure"):
    for source_path in source_root.rglob("*"):
        if source_path.is_file() and source_path.relative_to(repository_root) != allowed_placeholder_file:
            if placeholder.encode("utf-8") in source_path.read_bytes():
                raise SystemExit(
                    "learner placeholder outside AppSpec pinned transport contract: "
                    + str(source_path.relative_to(repository_root))
                )
PY
