def get(args, binding, engine):
    state = engine.action_state(args, binding)
    return {"status": "ok", **state}


def run(args, binding, engine):
    result = engine.action_run(args, binding)
    report = {
        "status": "ok",
        "action": binding.get(
            "action", binding.get("algorithm", binding.get("procedure", "action"))),
    }
    if isinstance(result, dict):
        report.update(result)
    return report
