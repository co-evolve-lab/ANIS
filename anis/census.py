import argparse
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import runtime

def app_command(app):
    script = os.path.join(_ROOT, "apps", app, "census.py")
    try:
        command = runtime.build_command(app, script)
        env = runtime.build_env(app)
    except runtime.RuntimeError_ as error:
        raise SystemExit(str(error))
    if not os.path.isfile(script):
        raise SystemExit(
            f"Missing {script}(application census adapter).\n"
            "The model generates it using prompts/census_adapter.md .")
    return command, env


def main():
    ap = argparse.ArgumentParser(description="ANIS S1 Application capability census dispatcher")
    ap.add_argument("--app", required=True,
                    help="Application name. Choices are determined by apps/*/runtime.json ; "
                         f"available: {', '.join(runtime.available_apps()) or 'none'}")
    args = ap.parse_args()

    cmd, env = app_command(args.app)
    print(f"[census] {args.app}: {' '.join(cmd[:2])} ...", flush=True)
    r = subprocess.run(cmd, env=env, cwd=_ROOT)
    if r.returncode != 0:
        raise SystemExit(f"[census] {args.app} failed with exit code {r.returncode}")

    out = os.path.join(_ROOT, "apps", args.app, "census", "raw_ops.json")
    if not os.path.isfile(out):
        raise SystemExit(f"[census] {args.app} did not produce {out}")
    d = json.load(open(out))
    print(f"[census] {args.app} Completed: engine {d.get('engine_version')}, "
          f"channel {sorted(d.get('channels', {}))}, "
          f"unavailable {len(d.get('unavailable', []))} items -> {out}")


if __name__ == "__main__":
    main()
