import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "stages" / "06_goal_commands" / "workflow.py"
_spec = importlib.util.spec_from_file_location("goal_workflow", STAGE)
workflow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(workflow)

ENGINE = '''import json
def observe(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
def make_demo_doc(path, text=None, table=None, force=False):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({}, handle)
'''
IMPL = '''import json
def run(args):
    with open(args["file"], encoding="utf-8") as handle:
        doc = json.load(handle)
    {body}
    with open(args["file"], "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    return {{"status": "ok"}}
'''
COMMANDS = {
    "doc-set": ({"key": {"type": "string", "required": True},
                 "value": {"type": "string", "required": True}},
                'doc[args["key"]] = args["value"]', "primitive"),
    "doc-tag": ({"label": {"type": "string", "required": True}},
                'doc.setdefault("tags", []).append(args["label"])', "primitive"),
    "doc-read": ({}, "pass", "observation"),
}
REQUESTS = [
    [("doc-set", {"key": "a", "value": "1"}), ("doc-set", {"key": "b", "value": "2"}),
     ("doc-tag", {"label": "x"})],
    [("doc-read", {}), ("doc-set", {"key": "c", "value": "3"}), ("doc-tag", {"label": "y"})],
    [("doc-set", {"key": "d", "value": "4"}), ("doc-tag", {"label": "z"}),
     ("doc-read", {})],
]


def make_package(root: Path) -> Path:
    package = root / "dist" / "demo-anis"
    (package / "anis").mkdir(parents=True)
    for name in ("__init__.py", "kernel.py", "cli.py"):
        shutil.copy2(ROOT / "anis" / name, package / "anis" / name)
    (root / "anis").mkdir()
    shutil.copy2(ROOT / "anis" / "goal.py", root / "anis" / "goal.py")
    (package / "engine.py").write_text(ENGINE, encoding="utf-8")
    for name, (args, body, layer) in COMMANDS.items():
        directory = package / "commands" / name
        directory.mkdir(parents=True)
        spec = {"command": name, "summary": name, "layer": layer, "object": "doc",
                "action": name.split("-", 1)[1],
                "args": {"file": {"type": "path", "required": True}, **args}}
        (directory / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        (directory / "impl.py").write_text(IMPL.format(body=body), encoding="utf-8")
    return package


def cli(package: Path, argv: list[str], cwd: Path, log: Path | None = None):
    env = {**os.environ, "PYTHONPATH": str(package)}
    env.pop("ANIS_CALL_LOG", None)
    if log:
        env["ANIS_CALL_LOG"] = str(log)
    done = subprocess.run([sys.executable, str(package / "anis" / "cli.py"), "--app",
                           str(package), *argv], cwd=cwd, env=env, capture_output=True, text=True)
    return json.loads(done.stdout)


def record_requests(root: Path, package: Path) -> Path:
    lines = []
    for index, calls in enumerate(REQUESTS):
        pristine = root / f"input{index}"
        pristine.mkdir()
        (pristine / "doc.json").write_text("{}", encoding="utf-8")
        served = root / f"served{index}"
        shutil.copytree(pristine, served)
        log = root / f"calls{index}.jsonl"
        for command, values in calls:
            argv = [command, "--file", "doc.json"]
            for key, value in values.items():
                argv += [f"--{key}", value]
            cli(package, argv, served, log)
        lines.append(json.dumps({"id": f"r{index}", "request": f"Request {index}",
                                 "workdir": pristine.name, "call_log": log.name}))
    path = root / "requests.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def answer(**fields):
    return {"shared_subgoal": True, "subgoal": "label a record", "object": "doc",
            "action": "record-add", "summary": "Set fields and tag the document.",
            "reason": "same change", **fields}


class GoalCommandTests(unittest.TestCase):
    def test_recorded_requests_yield_a_verified_goal_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = make_package(root)
            requests = workflow.load_requests(record_requests(root, package))
            self.assertEqual([call["command"] for call in requests[1]["calls"]],
                             ["doc-read", "doc-set", "doc-tag"])
            prompts = []
            with mock.patch.object(workflow, "ANIS_ROOT", root):
                decisions, released = workflow.synthesize(
                    "demo", requests, lambda prompt: prompts.append(prompt) or answer(),
                    min_support=3, max_length=5, max_requests=6, verify_timeout=60)
            self.assertEqual([row["sequence"] for row in decisions], [["doc-set", "doc-tag"]])
            self.assertIn("[3] Request 2", prompts[0])
            self.assertEqual([row["pass"] for row in decisions[0]["verification"]], [True] * 3)
            name, spec, contract = released[0]
            self.assertEqual((name, spec["layer"]), ("doc-record-add", "goal"))
            self.assertEqual(contract["steps"][0]["mode"], "batch")

            shutil.copy2(root / "anis" / "goal.py", package / "anis" / "goal.py")
            workflow.write_command(package / "commands" / name, spec, contract)
            (root / "fresh.json").write_text("{}", encoding="utf-8")
            result = cli(package, [name, "--file", "fresh.json", "--items",
                                   '[{"key":"k","value":"v"}]', "--label", "t"], root)
            self.assertEqual(result["steps_completed"], ["doc-set", "doc-tag"])
            self.assertEqual(json.loads((root / "fresh.json").read_text()),
                             {"k": "v", "tags": ["t"]})

            broken = {"shared": contract["shared"], "steps": contract["steps"][:1]}
            broken_spec = {**spec, "args": {k: v for k, v in spec["args"].items() if k != "label"}}
            workflow.write_command(package / "commands" / "doc-broken", broken_spec, broken)
            specs = workflow.load_specs(package)
            calls = workflow.mine(requests, specs, 3, 5)[0]["occurrences"][0]["calls"]
            result = workflow.verify(package, requests[0], calls, "doc-broken", broken, specs, 60)
            self.assertFalse(result["pass"])
            self.assertIn("saved state differs", result["reason"])

    def test_review_rejects_unshared_goals_and_retries_bad_names(self):
        candidate = {"sequence": ["doc-set", "doc-tag"], "requests": ["r0"],
                     "occurrences": [{"request": "r0", "calls": [
                         {"command": "doc-set", "args": {"file": "d"}},
                         {"command": "doc-tag", "args": {"file": "d"}}]}]}
        specs = {"doc-set": {"object": "doc"}, "doc-tag": {"object": "doc"}}
        texts = {"r0": "Request 0"}

        name, record = workflow.review("demo", candidate, texts, specs, set(),
                                       lambda prompt: answer(shared_subgoal=False), 6)
        self.assertIsNone(name)
        self.assertFalse(record["shared_subgoal"])

        prompts, replies = [], iter([answer(action="set"), answer(action="label")])
        name, _ = workflow.review("demo", candidate, texts, specs, {"doc-set"},
                                  lambda prompt: prompts.append(prompt) or next(replies), 6)
        self.assertEqual(name, "doc-label")
        self.assertIn("`doc-set` is already an existing command", prompts[1])

        name, record = workflow.review("demo", candidate, texts, specs, set(),
                                       lambda prompt: None, 6)
        self.assertIsNone(name)
        self.assertEqual(len(record["answers"]), 2)


if __name__ == "__main__":
    unittest.main()
