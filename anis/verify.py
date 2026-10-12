import argparse
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import hashlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from anis import app_contract, kernel, runtime  # noqa: E402

RUN_TIMEOUT = 300


def cli(app_dir, argv, timeout=RUN_TIMEOUT):
    env = dict(os.environ)
    env["PYTHONPATH"] = _ROOT + os.pathsep + app_dir
    r = subprocess.run([sys.executable, os.path.join(_ROOT, "anis", "cli.py"),
                        "--app", app_dir] + argv,
                       capture_output=True, text=True, env=env, timeout=timeout)
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        out = {"status": "error", "code": "BAD_ENVELOPE",
               "message": r.stdout[-500:] + r.stderr[-500:]}
    return r.returncode, out


def load_engine(app, app_dir):
    path = os.path.join(app_dir, "engine.py")
    spec = importlib.util.spec_from_file_location(f"anis_verify_engine_{app}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def census_symbols(app):
    path = os.path.join(_ROOT, "apps", app, "census", "raw_ops.json")
    d = json.load(open(path, encoding="utf-8"))
    out = {}

    def flatten(prefix, node):
        if isinstance(node, list):
            names = set()
            for it in node:
                if isinstance(it, str):
                    names.add(it)
                elif isinstance(it, dict):
                    names.add(
                        it.get("name") or it.get("cmd")
                        or it.get("service") or "")
            out[prefix] = {n for n in names if n}
        elif isinstance(node, dict):
            for k, v in node.items():
                flatten(f"{prefix}.{k}" if prefix else k, v)

    flatten("", d.get("channels", {}))

    attested = attested_symbols(app)
    if attested:
        out["attested"] = set(attested)
        smuggled = sorted(
            symbol for symbol in attested
            for pool in out.values() if symbol in pool and pool is not out["attested"])
        if smuggled:
            raise SystemExit(
                f"[verify] {app}: Manually attested symbols also appear in the automated census: "
                f"{smuggled[:5]}\n"
                "        Only the census script may write census output. Put attested capabilities in attested.json, "
                "instead of raw_ops.jsonto preserve the distinction from runtime-reported capabilities.")
    return out


def attested_symbols(app):
    path = os.path.join(_ROOT, "apps", app, "census", "attested.json")
    if not os.path.isfile(path):
        return {}
    data = json.load(open(path, encoding="utf-8"))
    out = {}
    for entry in data.get("capabilities", []):
        symbol = entry.get("symbol")
        if not symbol:
            continue
        if not str(entry.get("reason", "")).strip():
            raise SystemExit(
                f"[verify] {app}: attested.json entry {symbol!r} is missing a reason. "
                "Each attested capability must explain why automated census cannot cover it.")
        out[symbol] = entry
    return out


def gate_structure(cmd_dir, spec, app_dir):
    missing = [k for k in ("command", "summary", "args", "errors", "demo")
               if k not in spec]
    if missing:
        return False, f"spec is missing fields {missing}"
    for p in (_ROOT, app_dir):
        if p not in sys.path:
            sys.path.insert(0, p)
    from anis import kernel
    try:
        kernel.load_impl(cmd_dir)
    except Exception as e:
        return False, f"impl Loading failed: {e}"
    return True, "spec complete; single run() is loadable"


def gate_hallucination(spec, symbols):
    ref = spec.get("census_ref")
    if not ref:
        return False, "Missing census_ref(commands must trace to census entries)"

    pool = symbols.get(ref["channel"], set())
    if ref["symbol"] in pool:
        if ref["channel"] == "attested":
            return True, (f"{ref['symbol']} comes from the attestation list attested.json, "
                          "is not an automated census result")
        return True, f"{ref['symbol']} ∈ {ref['channel']}"


    if ref["symbol"] in symbols.get("attested", set()):
        return True, (f"{ref['symbol']} comes from the attestation list attested.json "
                      f"(recorded channel {ref['channel']!r} no longer exists; "
                      "change census_ref.channel to \"attested\")")

    return False, (f"{ref['symbol']} is not in census channel {ref['channel']} "
                   f"({len(pool)} entries) or the attestation list")


def values_match(cur, expect):
    if isinstance(expect, bool) or isinstance(cur, bool):
        return cur == expect
    if isinstance(expect, (int, float)) and isinstance(cur, (int, float)):
        return abs(cur - expect) <= 1e-3 * max(1.0, abs(expect))
    if isinstance(expect, dict) and isinstance(cur, dict):
        return set(cur) == set(expect) and all(
            values_match(cur[k], expect[k]) for k in expect)
    if isinstance(expect, list) and isinstance(cur, list):
        return len(cur) == len(expect) and all(
            values_match(c, e) for c, e in zip(cur, expect))
    return cur == expect


def make_demo(app_dir, spec, demo):
    argv = ["make-demo", "--file", demo, "--force", "true",
            "--text", spec["demo"].get("text", "Demo text")]
    # Demo creation is a test precondition, so retry it once before failing the
    # command under test.
    for attempt in range(2):
        code, out = cli(app_dir, argv)
        if code == 0:
            break
    if code != 0:
        return False, f"make-demo failed twice in a row: {out.get('message')}"
    return True, "Demo file created"


def command_argv(spec, demo):
    return ([spec["command"], "--file", demo]
            + kernel.argument_tokens(spec["demo"]["args"]))


def getter_argv(getter, setter_spec, demo, contract):
    selectors = contract["build"].get("selector_args", {}).get(
        setter_spec["binding"]["kind"], {})
    return ([getter, "--file", demo]
            + kernel.argument_tokens(setter_spec["demo"]["args"], selectors))


def _content_digest(path, adapter=None):
    if adapter is not None and hasattr(adapter, "content_digest"):
        return adapter.content_digest(path)
    digest = hashlib.sha256()
    digest.update(open(path, "rb").read())
    return digest.hexdigest()


def check_verb(app, app_dir, spec, demo, run_report, before_digest=None,
               adapter=None):

    code, observed = cli(app_dir, ["observe", "--file", demo])
    if code != 0:
        return False, f"observe failed: {observed.get('message')}"
    state = observed["state"]
    expect = spec["demo"]["expect"]
    if expect.get("content_changed"):
        if before_digest is None or _content_digest(demo, adapter) == before_digest:
            return False, "File contents unchanged after saving and reopening"
    if "state" in expect:
        actual = json.loads(json.dumps(state))
        actual.pop("file", None)
        if not values_match(actual, expect["state"]):
            return False, "Reopened state differs from the frozen action probe state"
    if "content_fingerprint" in expect:
        if _content_digest(demo, adapter) != expect["content_fingerprint"]:
            return False, "Reopened content digest differs from action probe evidence"
    if "action_state" in expect:
        engine = load_engine(app, app_dir)
        actual = engine.action_state({"file": demo}, spec["binding"])["current"]
        wanted = json.loads(json.dumps(expect["action_state"]))
        actual = json.loads(json.dumps(actual))
        if adapter is not None and hasattr(adapter, "normalize_action_state"):
            actual, wanted = adapter.normalize_action_state(
                spec, actual, wanted)
        if not values_match(actual, wanted):
            return False, "Reopened operation state differs from the frozen probe state"
    if "output_exists" in expect:
        output = run_report.get("output")
        if not output or not os.path.isfile(output):
            return False, f"Action did not produce an output file: {output!r}"
        return True, f"Output file exists: {output}"
    if adapter is not None and hasattr(adapter, "check_verb_expectation"):
        return adapter.check_verb_expectation(
            spec, state, run_report, expect, demo)
    return True, "observe confirmed the persisted command effect"


def external_state(app, app_dir, spec, demo, engine, contract):
    kind = spec["binding"]["kind"]
    getter = (spec.get("related") or {}).get("read_current")
    if getter:
        code, out = cli(
            app_dir, getter_argv(getter, spec, demo, contract))
        if code != 0:
            raise RuntimeError(f"getter {getter} failed: {out.get('message')}")
        return out.get("current")
    args = {"file": demo}
    args.update(spec["demo"]["args"])
    if kind == "transform":
        return engine.measure(args)
    if kind == "processing":
        return engine.action_state(args, spec["binding"]).get("current")
    if kind in contract["build"].get("selector_args", {}):
        from anis import proptemplate
        return proptemplate.get(args, spec["binding"], engine)["current"]
    raise RuntimeError(f"No {app}/{kind} external observation is defined")


def gate_recipe(app, app_dir, spec, tmp, engine, contract, adapter):
    demo = os.path.join(tmp, "demo" + runtime.document_extension(app))
    ok, msg = make_demo(app_dir, spec, demo)
    if not ok:
        return False, msg, demo
    for setup in spec["demo"].get("setup_commands", []):
        argv = [part.replace("{file}", demo) for part in shlex.split(setup)]
        code, out = cli(app_dir, argv)
        if code != 0:
            return False, (f"Action prerequisite command failed: {setup}: "
                           f"{out.get('message')}"), demo


    if spec.get("paired_setter"):
        setter_dir = os.path.join(app_dir, "commands", spec["paired_setter"])
        setter_spec = json.load(open(os.path.join(setter_dir, "spec.json"),
                                     encoding="utf-8"))
        code, out = cli(app_dir, command_argv(setter_spec, demo))
        if code != 0:
            return False, f"paired setter failed: {out.get('message')}", demo
        code, got = cli(app_dir, getter_argv(
            spec["command"], setter_spec, demo, contract))
        if code != 0 or not values_match(got.get("current"), spec["demo"]["expect"]):
            return False, (f"getter read {got.get('current')!r}, "
                           f"expected {spec['demo']['expect']!r}"), demo
        return True, "Write followed by read; getter returns the actual current value", demo

    demo_expect = spec["demo"].get("expect")
    before_digest = (
        _content_digest(demo, adapter)
        if isinstance(demo_expect, dict) and demo_expect.get("content_changed")
        else None)
    argv = command_argv(spec, demo)
    observation_digest = (
        _content_digest(demo, adapter)
        if spec["binding"]["kind"] == "observation" else None)
    code, run1 = cli(app_dir, argv)
    if code != 0 or run1.get("status") != "ok":
        return False, f"Command failed: {run1.get('code')} {run1.get('message')}", demo

    kind = spec["binding"]["kind"]
    if kind == "observation":
        expected = spec["demo"]["expect"]
        if not values_match(run1.get("current"), expected):
            return False, (f"Read-only observation {run1.get('current')!r}, "
                           f"expected {expected!r}"), demo
        code, run2 = cli(app_dir, argv)
        if code != 0 or not values_match(run2.get("current"), expected):
            return False, "Second read-only observation is unstable", demo
        if _content_digest(demo, adapter) != observation_digest:
            return False, "Read-only observation modified the input file", demo
        return True, "Read-only observations match frozen expectations and remain stable", demo
    if kind == "verb":
        ok, detail = check_verb(
            app, app_dir, spec, demo, run1, before_digest=before_digest,
            adapter=adapter)
        if not ok:
            return False, detail, demo
        if spec["demo"].get("repeat_mode") == "fresh":
            os.remove(demo)
            ok, msg = make_demo(app_dir, spec, demo)
            if not ok:
                return False, f"Second demo file creation failed: {msg}", demo
            for setup in spec["demo"].get("setup_commands", []):
                setup_argv = [
                    part.replace("{file}", demo) for part in shlex.split(setup)]
                code, out = cli(app_dir, setup_argv)
                if code != 0:
                    return False, (f"Second demo prerequisite failed: "
                                   f"{out.get('message')}"), demo
        code, run2 = cli(app_dir, argv)
        if code != 0:
            return False, f"Second consecutive execution failed: {run2.get('message')}", demo
        repeat = ("Execution succeeded on two equivalent inputs"
                  if spec["demo"].get("repeat_mode") == "fresh"
                  else "Two consecutive executions succeeded")
        return True, detail + f"; {repeat}", demo

    try:
        cur1 = external_state(app, app_dir, spec, demo, engine, contract)
    except Exception as e:
        return False, f"External observation failed: {e}", demo
    expect = spec["demo"]["expect"]
    expected1 = expect["after1"] if kind == "transform" else expect
    if not values_match(cur1, expected1):
        return False, f"External observation {cur1!r}; expected {expected1!r}", demo

    code, run2 = cli(app_dir, argv)
    if code != 0:
        return False, f"Second consecutive execution failed: {run2.get('message')}", demo
    cur2 = external_state(app, app_dir, spec, demo, engine, contract)
    expected2 = expect["after2"] if kind == "transform" else expected1
    if not values_match(cur2, expected2):
        return False, f"Second external observation {cur2!r}; expected {expected2!r}", demo
    return True, "External observations match frozen expectations across two successful executions", demo


def gate_badargs(app_dir, spec, demo):
    code, _ = cli(app_dir, [spec["command"], "--file", demo, "--bogus", "1"])
    if code != 2:
        return False, f"Unknown argument exit code {code}(expected 2)"
    required = [k for k, a in spec["args"].items()
                if a.get("required") and k != "file"]
    if required:
        argv = [spec["command"], "--file", demo]
        for k, v in spec["demo"]["args"].items():
            if k != required[0] and k in spec["args"]:
                argv += [f"--{k}", str(v)]
        code, _ = cli(app_dir, argv)
        if code != 2:
            return False, f"Missing required --{required[0]} exit code {code}(expected 2)"
    return True, "Unknown and missing required arguments both return exit code 2 rejected"


def verify_one(app, app_dir, cmd_root, symbols, contract, adapter, name):
    cmd_dir = os.path.join(cmd_root, name)
    spec_path = os.path.join(cmd_dir, "spec.json")
    if not os.path.isfile(spec_path):
        return name, None
    spec = json.load(open(spec_path, encoding="utf-8"))
    gates = {}
    ok, msg = gate_structure(cmd_dir, spec, app_dir)
    gates["structure"] = {"pass": ok, "detail": msg}
    if ok:
        ok, msg = gate_hallucination(spec, symbols)
        gates["hallucination"] = {"pass": ok, "detail": msg}
        if ok:
            engine = load_engine(app, app_dir)
            with tempfile.TemporaryDirectory() as tmp:
                ok, msg, demo = gate_recipe(
                    app, app_dir, spec, tmp, engine, contract, adapter)
                gates["external_observation"] = {"pass": ok, "detail": msg}
                ok2, msg2 = gate_badargs(app_dir, spec, demo)
                gates["badargs"] = {"pass": ok2, "detail": msg2}
    return name, {"pass": all(g["pass"] for g in gates.values()), "gates": gates}


def main():
    ap = argparse.ArgumentParser(description="ANIS Command external verification")
    ap.add_argument("--app", required=True)
    ap.add_argument("--only")
    ap.add_argument("--jobs", type=int, default=1,
                    help="Parallel jobs; commands run in independent temporary directories")
    args = ap.parse_args()
    app_dir = os.path.join(_ROOT, "apps", args.app)
    contract = app_contract.load(args.app)
    adapter = app_contract.load_adapter(args.app, contract)
    maximum = contract["verification"].get("max_parallel_jobs")
    if isinstance(maximum, int) and maximum > 0:
        args.jobs = min(args.jobs, maximum)
    census_path = os.path.join(app_dir, "census", "raw_ops.json")
    census = json.load(open(census_path, encoding="utf-8"))
    if adapter and hasattr(adapter, "check_verification"):
        adapter.check_verification(census)
    cmd_root = os.path.join(app_dir, "commands")
    symbols = census_symbols(args.app)
    names = [args.only] if args.only else sorted(os.listdir(cmd_root))

    report = {}
    if args.jobs <= 1:
        for name in names:
            name, result = verify_one(
                args.app, app_dir, cmd_root, symbols, contract, adapter, name)
            if result is not None:
                report[name] = result
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futures = {ex.submit(
                verify_one, args.app, app_dir, cmd_root, symbols,
                contract, adapter, name): name for name in names}
            for future in as_completed(futures):
                name, result = future.result()
                if result is not None:
                    report[name] = result

    for name in sorted(report):
        result = report[name]
        mark = "PASS" if result["pass"] else "FAIL"
        print(f"[{mark}] {name}")
        for gate, detail in result["gates"].items():
            if not detail["pass"]:
                print(f"   ✗ {gate}: {detail['detail']}")

    selected_report = dict(report)
    out_dir = os.path.join(app_dir, "build")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "report.json")
    if args.only and os.path.isfile(out_path):
        old = json.load(open(out_path, encoding="utf-8"))
        old.update(report)
        report = old
    json.dump(report, open(out_path, "w"), indent=1, ensure_ascii=False)
    n_pass = sum(1 for result in report.values() if result["pass"])
    print(f"\n[verify] {args.app}: {n_pass}/{len(report)} passed all checks -> {out_path}")
    # --only is a targeted repair gate.  Historical failures remain in the
    # cumulative report for visibility, but must not make an unrelated repaired
    # command fail its own gate.
    selected_pass = sum(
        1 for result in selected_report.values() if result["pass"])
    if selected_pass != len(selected_report):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
