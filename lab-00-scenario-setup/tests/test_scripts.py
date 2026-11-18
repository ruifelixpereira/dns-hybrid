import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_lab", ROOT / "scripts/verify.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class RunCommandTests(unittest.TestCase):
    def test_requires_execution_success_and_zero_exit(self):
        for state, code in (("Failed", 0), ("Succeeded", 1), ("Running", None)):
            with self.subTest(state=state, code=code), self.assertRaises(RuntimeError):
                verify.assert_run_succeeded({
                    "properties": {"instanceView": {"executionState": state, "exitCode": code}}
                })
        with self.assertRaises(RuntimeError):
            verify.assert_run_succeeded({"properties": {}})
        with contextlib.redirect_stdout(io.StringIO()):
            verify.assert_run_succeeded({
                "properties": {"instanceView": {"executionState": "Succeeded", "exitCode": 0}}
            })


class DeploymentSafetyTests(unittest.TestCase):
    def run_deploy(self, action, input_text, actual_tenant="expected-tenant"):
        with tempfile.TemporaryDirectory() as directory:
            az = Path(directory) / "az"
            az.write_text(
                "#!/bin/sh\n"
                'if [ "$1 $2" = "account show" ]; then\n'
                '  printf "%s\\n" "$FAKE_TENANT"\n'
                "else\n"
                '  printf "%s\\n" "$@" > "$FAKE_ARGS"\n'
                '  test "$DNS_ADMIN_PASSWORD" = "$FAKE_PASSWORD" || exit 9\n'
                '  echo "mock deployment command"\n'
                "fi\n"
            )
            az.chmod(0o700)
            args = Path(directory) / "args"
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/deploy.sh"), action, "tenant-a",
                 "expected-tenant", "expected-sub"],
                input=input_text, text=True, capture_output=True,
                env={
                    **os.environ,
                    "PATH": directory + os.pathsep + os.environ["PATH"],
                    "FAKE_TENANT": actual_tenant,
                    "FAKE_ARGS": str(args),
                    "FAKE_PASSWORD": "Example-only-password-123!",
                },
            )
            return result, args.read_text().splitlines() if args.exists() else None

    def test_tenant_mismatch_stops_before_deployment(self):
        result, args = self.run_deploy("create", "", actual_tenant="wrong-tenant")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Tenant mismatch", result.stderr)
        self.assertIsNone(args)

    def test_create_requires_exact_confirmation(self):
        result, args = self.run_deploy("create", "no\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cancelled", result.stderr)
        self.assertIsNone(args)

    def test_actions_never_leak_password_into_arguments_or_output(self):
        for action in ("validate", "what-if", "create"):
            input_text = "Example-only-password-123!\n"
            if action == "create":
                input_text = "tenant-a/expected-sub\n" + input_text
            with self.subTest(action=action):
                result, args = self.run_deploy(action, input_text)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(args[:3], ["deployment", "sub", action])
                self.assertIn("--subscription", args)
                self.assertIn("expected-sub", args)
                self.assertNotIn("Example-only-password-123!", "\n".join(args) + result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
