import json
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from anis import bootstrap, onboard, observatory, system_config


ROOT = Path(__file__).resolve().parents[1]


class ReleaseArtifactTests(unittest.TestCase):
    def test_default_configuration_is_valid(self):
        config = system_config.load(ROOT / "anis-config.json")
        self.assertEqual(config["app_aliases"], {})

    def test_workflow_has_four_public_stages(self):
        phases = onboard.workflow_stages(
            "example", "example", 1.0,
            system_config.load(ROOT / "anis-config.json"),
        )
        self.assertEqual(len(phases), 4)
        self.assertEqual(sum(len(phase["steps"]) for phase in phases), 14)

    def test_stage_configuration_contains_no_bundled_apps(self):
        payload = json.loads((
            ROOT / "stages" / "01_capability_discovery" / "config.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual(payload["apps"], {})

    def test_release_runtime_has_no_named_software_branches(self):
        forbidden = re.compile(
            r"\b(blender|libreoffice|writer|calc|impress|qgis|gimp|ooxml|uno)\b",
            re.IGNORECASE,
        )
        files = list((ROOT / "anis").glob("*.py"))
        files.extend((ROOT / "stages").glob("**/*.py"))
        hits = {
            str(path.relative_to(ROOT)): forbidden.findall(
                path.read_text(encoding="utf-8"))
            for path in files
            if forbidden.search(path.read_text(encoding="utf-8"))
        }
        self.assertEqual(hits, {})

    def test_plan_entrypoint_runs_without_an_application_adapter(self):
        result = subprocess.run(
            [str(ROOT / "bin" / "anis-release"), "plan",
             "--app", "example", "--launch", "example"],
            cwd=ROOT, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Stage 4", result.stdout)

    def test_custom_config_reaches_command_preparation(self):
        config = system_config.load(ROOT / "anis-config.json")
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            return True, "ok"

        with mock.patch.object(onboard, "run_script", side_effect=fake_run):
            onboard.ACTIVE_CONFIG = config
            passed, _ = onboard.run_release(
                "example", config, "test-run")
        self.assertTrue(passed)
        self.assertIn("--config", commands[0])
        self.assertIn(config["_path"], commands[0])

    def test_bootstrap_accepts_an_evidenced_local_launcher(self):
        payload = {
            "app": "example",
            "status": "found",
            "launch_command": sys.executable,
            "entrypoint_kind": "console",
            "evidence": ["The executable exists and returned a version."],
            "alternatives": [],
            "notes": "",
        }
        self.assertEqual(
            bootstrap.validate_result(payload, "example"), payload)

    def test_bootstrap_accepts_a_model_normalized_natural_request(self):
        payload = {
            "status": "ready",
            "action": "anisize",
            "app": "example-app",
            "display_name": "Example Application",
            "request_summary": "Create ANIS capabilities for the application.",
            "confidence": 0.94,
            "clarification": "",
        }
        self.assertEqual(bootstrap.validate_intent(payload), payload)

    def test_bootstrap_preserves_model_clarification(self):
        payload = {
            "status": "needs_clarification",
            "action": "anisize",
            "app": "",
            "display_name": "",
            "request_summary": "The request could name two applications.",
            "confidence": 0.4,
            "clarification": "Which application did you mean?",
        }
        self.assertEqual(bootstrap.validate_intent(payload), payload)

    def test_bootstrap_rejects_an_invented_launcher(self):
        payload = {
            "app": "example",
            "status": "found",
            "launch_command": "/definitely/not/an/application",
            "entrypoint_kind": "console",
            "evidence": ["Unverified claim."],
            "alternatives": [],
            "notes": "",
        }
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.validate_result(payload, "example")

    def test_bootstrap_forwards_only_validated_hint_to_existing_workflow(self):
        command = bootstrap.downstream_command(
            "example", sys.executable,
            str(ROOT / "anis-config.json"), fresh=True)
        self.assertEqual(command[0], str(ROOT / "bin" / "anis-release"))
        self.assertIn("--launch", command)
        self.assertEqual(command[-2:], ["--", "--no-resume"])

    def test_bootstrap_has_no_fixed_application_vocabulary(self):
        source = (ROOT / "anis" / "bootstrap.py").read_text(encoding="utf-8")
        self.assertNotIn("known_apps", source)
        self.assertNotIn("APP_ALIASES", source)

    def test_natural_language_plan_starts_existing_anis_controller(self):
        intent = {
            "status": "ready", "action": "anisize", "app": "example-app",
            "display_name": "Example Application", "request_summary": "Anisize it.",
            "confidence": 0.98, "clarification": "",
        }
        entry = {
            "app": "example-app", "status": "found",
            "launch_command": sys.executable, "entrypoint_kind": "console",
            "evidence": ["Local executable verified."], "alternatives": [],
            "notes": "",
        }
        config = system_config.load(ROOT / "anis-config.json")
        process = mock.Mock(pid=43210)
        process.wait.return_value = 0
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                bootstrap.system_config, "load", return_value=config), mock.patch.object(
                bootstrap, "new_request_run", return_value=Path(directory)), mock.patch.object(
                bootstrap, "interpret_request", return_value=intent), mock.patch.object(
                bootstrap, "discover", return_value=(entry, Path(directory))), mock.patch.object(
                bootstrap.subprocess, "Popen", return_value=process) as launched, mock.patch.object(
                sys, "argv", ["anis-bootstrap", "--no-monitor",
                              "Please make this app usable by an Agent"]):
            self.assertEqual(bootstrap.main(), 0)
        command = launched.call_args.args[0]
        self.assertEqual(command[0], str(ROOT / "bin" / "anis-release"))
        self.assertIn("example-app", command)

    def test_observatory_exposes_bootstrap_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "request.txt").write_text(
                "Please onboard an application.\n", encoding="utf-8")
            bootstrap.report_progress(
                run_dir, "intent", "running", "Understanding request")
            payload = observatory.snapshot(run_dir)
        self.assertEqual(payload["progress"][0]["step"], "intent")
        self.assertEqual(payload["progress"][0]["status"], "running")

    def test_observatory_selects_a_local_port(self):
        port = bootstrap.available_port(18765)
        self.assertGreaterEqual(port, 18765)
        self.assertLess(port, 18815)


if __name__ == "__main__":
    unittest.main()
