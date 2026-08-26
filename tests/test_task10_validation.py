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

    def test_candidate_gate_accepts_the_pinned_source_and_rejects_sha_drift(self):
        accepted = self.run_gate()
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        template = self.candidate_root / "infrastructure/template.yaml"
        template.write_text(
            template.read_text(encoding="utf-8").replace(
                "V2_SHA256=" + V2_SHA256,
                "V2_SHA256=" + V2_SHA256 + "0",
                1,
            ),
            encoding="utf-8",
        )

        rejected = self.run_gate()
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("pinned asset mismatch", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
