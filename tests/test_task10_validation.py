import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
V2_SHA256 = "2027a77a46868f6ac0a806d6cf0c5971fddcc72e8196009a507248fedcce72de"


@unittest.skipIf(
    os.environ.get("VALIDATION_INNER") == "1",
    "the outer validation-contract test must not recursively invoke the gate",
)
class Task10ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.candidate_root = Path(self.temporary_directory.name) / "candidate"
        shutil.copytree(
            REPOSITORY_ROOT,
            self.candidate_root,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache"),
        )
        self._commit_baseline()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _commit_baseline(self):
        for command in (
            ["git", "init", "-q"],
            ["git", "config", "user.name", "validation test"],
            ["git", "config", "user.email", "validation@example.invalid"],
            ["git", "add", "--all"],
            ["git", "commit", "-qm", "baseline"],
        ):
            subprocess.run(command, cwd=self.candidate_root, check=True)

    def run_gate(self):
        environment = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        environment.pop("VALIDATION_INNER", None)
        return subprocess.run(
            ["bash", "scripts/validate.sh"],
            cwd=self.candidate_root,
            capture_output=True,
            text=True,
            env=environment,
        )

    def assert_clean_candidate_passes(self):
        accepted = self.run_gate()
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def replace_template(self, old, new):
        template = self.candidate_root / "infrastructure/template.yaml"
        source = template.read_text(encoding="utf-8")
        self.assertIn(old, source)
        template.write_text(source.replace(old, new, 1), encoding="utf-8")

    def test_candidate_gate_accepts_the_pinned_source_and_rejects_sha_drift(self):
        self.assert_clean_candidate_passes()
        self.replace_template(
            "V2_SHA256=" + V2_SHA256,
            "V2_SHA256=" + V2_SHA256 + "0",
        )

        rejected = self.run_gate()
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("pinned asset mismatch", rejected.stderr)

    def test_candidate_gate_rejects_a_mismapped_download_checksum(self):
        self.assert_clean_candidate_passes()
        self.replace_template(
            'download_asset "$V2_ASSET" "$V2_SHA256" "$V2_ARCHIVE" 0644',
            'download_asset "$V2_ASSET" "$APPSPEC_SHA256" "$V2_ARCHIVE" 0644',
        )

        rejected = self.run_gate()
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("pinned transport mismatch: V2", rejected.stderr)

    def test_candidate_gate_rejects_broadened_iam_permissions(self):
        self.assert_clean_candidate_passes()
        self.replace_template(
            "      RoleName: globomantics-orders-codedeploy-service-role\n",
            "      RoleName: globomantics-orders-codedeploy-service-role\n"
            "      Policies:\n"
            "        - PolicyName: forbidden-pass-role\n"
            "          PolicyDocument:\n"
            "            Version: '2012-10-17'\n"
            "            Statement:\n"
            "              - Effect: Allow\n"
            "                Action: iam:PassRole\n"
            "                Resource: '*'\n",
        )

        rejected = self.run_gate()
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("IAM boundary violation: iam:PassRole is forbidden", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
