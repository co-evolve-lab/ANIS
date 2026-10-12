import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from anis import onboard, observatory, system_config
from scripts.package_release import package


ROOT = Path(__file__).resolve().parents[1]


class SourceDistributionTests(unittest.TestCase):
    def test_archive_uses_manifest_and_neutral_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("Public source\n")
            (root / ".env").write_text("LOCAL_ONLY=private\n")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("local identity\n")
            data = (root / "README.md").read_bytes()
            (root / "MANIFEST.sha256").write_text(
                f"{hashlib.sha256(data).hexdigest()}  ./README.md\n")
            output = root / "release.tar.gz"
            self.assertEqual(package(root, output), 2)
            first = output.read_bytes()
            package(root, output)
            self.assertEqual(output.read_bytes(), first)
            with tarfile.open(output) as archive:
                self.assertEqual(set(archive.getnames()), {
                    "ANIS/README.md", "ANIS/MANIFEST.sha256"})
                for member in archive.getmembers():
                    self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
                    self.assertEqual((member.uname, member.gname), ("", ""))

    def test_archive_rejects_modified_source_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("original")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            manifest = root / "MANIFEST.sha256"
            manifest.write_text(f"{digest}  ./source.txt\n")
            source.write_text("changed")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                package(root, root / "release.tar.gz")
            link = root / "link.txt"
            link.symlink_to(source)
            manifest.write_text(f"{digest}  ./link.txt\n")
            with self.assertRaisesRegex(ValueError, "Symlink"):
                package(root, root / "release.tar.gz")

    def test_prompt_placeholders_are_resolved(self):
        for name in ("probe_runtime.md", "census_adapter.md", "build_app.md"):
            rendered = onboard.read_prompt(name, "example", None)
            self.assertNotIn("{app}", rendered)
            self.assertNotIn("{launch}", rendered)
        self.assertIn("{script}", onboard.read_prompt("probe_runtime.md", "example"))

    def test_monitor_step_names_match_controller(self):
        phases = onboard.workflow_stages("example", "example", 1.0, system_config.load())
        expected = [step["name"] for phase in phases for step in phase["steps"]]
        actual = [name for _, _, names in observatory.PHASES for name in names]
        self.assertEqual(actual, expected)

    def test_release_preparation_uses_current_discovery_run(self):
        commands = []
        with mock.patch.object(onboard, "run_script", side_effect=lambda cmd, **kw: (
                commands.append(cmd) or True, "ok")):
            onboard.run_release("example", system_config.load(), "current-run")
        command = commands[0]
        self.assertEqual(command[command.index("--discovery-run") + 1], "current-run")

    def test_standalone_preparation_does_not_collect_unselected_runs(self):
        stage = ROOT / "stages" / "02_05_command_release"
        with mock.patch.object(sys, "path", [str(stage), *sys.path]):
            spec = importlib.util.spec_from_file_location("release_prepare", stage / "prepare.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "discoveries" / "current-run"
            older = root / "discoveries" / "other-run"
            current.mkdir(parents=True)
            older.mkdir()
            for folder, symbol in ((current, "current-operation"), (older, "other-operation")):
                batch = folder / "batch"
                batch.mkdir()
                (folder / "state.json").write_text(json.dumps({"batches": [{
                    "path": "batch", "items": {symbol: {
                        "status": "accepted", "accepted_attempt": "batch"}}}]}))
                (batch / "batch.json").write_text(json.dumps({
                    "app": "example", "items": [{"item_id": symbol, "symbol": symbol}]}))
                (batch / "review_result.json").write_text(json.dumps({"items": [{
                    "item_id": symbol, "decision": "expose", "reason": "Useful operation"}]}))
            with mock.patch.object(module, "ANIS", root), mock.patch.object(
                    module, "RESULT_ROOTS", (root / "discoveries",)), mock.patch.object(
                    module, "__file__", str(root / "prepare.py")), mock.patch.object(
                    sys, "argv", ["prepare.py", "--run-id", "current-run"]), contextlib.redirect_stdout(io.StringIO()):
                module.main()
            manifest = json.loads((root / "runs/current-run/example/manifest.json").read_text())
            self.assertEqual([item["command"] for item in manifest["operations"]], ["current-operation"])

    def test_manifest_sources_have_no_cjk_text(self):
        manifest = ROOT / "MANIFEST.sha256"
        for line in manifest.read_text().splitlines():
            relative = line.split(maxsplit=1)[1].removeprefix("./")
            source = ROOT / relative
            with self.subTest(file=relative):
                self.assertIsNone(re.search(r"[\u3400-\u9fff]", source.read_text()))


if __name__ == "__main__":
    unittest.main()
