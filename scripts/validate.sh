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
    ("assets/function/v2.zip", "V2_ASSET", "V2_SHA256", "/home/cloud_user/lab/assets/function/v2.zip"),
    ("assets/appspec/release-v2.json", "APPSPEC_ASSET", "APPSPEC_SHA256", "/home/cloud_user/lab/appspec/release-v2.json"),
    ("assets/helpers/invoke-loop.py", "INVOKE_LOOP_ASSET", "INVOKE_LOOP_SHA256", "/home/cloud_user/lab/bin/invoke-loop"),
    ("assets/helpers/record-outcome.py", "RECORD_OUTCOME_ASSET", "RECORD_OUTCOME_SHA256", "/home/cloud_user/lab/bin/record-outcome"),
)
for asset_path, asset_variable, sha_variable, learner_path in assets:
    if assignments.get(asset_variable) != asset_path:
        raise SystemExit(f"pinned asset path mismatch: {asset_variable}")
    actual_sha = hashlib.sha256((repository_root / asset_path).read_bytes()).hexdigest()
    if assignments.get(sha_variable) != actual_sha:
        raise SystemExit(f"pinned asset mismatch: {asset_path}")
    learner_reference = "$LAB_ROOT" + learner_path.removeprefix(lab_root)
    if learner_reference not in user_data:
        raise SystemExit(f"required learner path missing: {learner_path}")

asset_base_url = assignments.get("ASSET_BASE_URL", "")
raw_url = re.fullmatch(
    r"https://raw\.githubusercontent\.com/pluralsight-cloud/"
    r"Lab-Release-and-Roll-Back-an-AWS-Cloud-Native-Workload/([0-9a-f]{40})",
    asset_base_url,
)
if raw_url is None:
    raise SystemExit("pinned GitHub raw URL must use an immutable commit")
pinned_commit = raw_url.group(1)
if not re.fullmatch(r"[0-9a-f]{40}", pinned_commit):
    raise SystemExit("pinned GitHub commit is not immutable")
for asset_path, _, _, _ in assets:
    expected_url = f"{asset_base_url}/{asset_path}"
    if not expected_url.startswith(asset_base_url + "/assets/"):
        raise SystemExit(f"pinned GitHub raw URL shape is invalid: {asset_path}")

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

deployment_group = template["Resources"]["OrdersDeploymentGroup"]["Properties"]
if "AlarmConfiguration" in deployment_group or "DEPLOYMENT_STOP_ON_ALARM" in deployment_group[
    "AutoRollbackConfiguration"
]["Events"]:
    raise SystemExit("CodeDeploy alarm wiring is forbidden")

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
