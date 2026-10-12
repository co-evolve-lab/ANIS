def normalize(raw, binding):
    vt = binding["value"]["type"]
    if raw is None:
        return None
    if vt == "bool":
        return raw > binding["value"].get("threshold", 0)
    if vt == "color":
        return "(automatic)" if raw in (-1, None) else f"#{int(raw) & 0xFFFFFF:06X}"
    if vt == "enum":
        names = binding["value"].get("names", {})
        if not names:
            return raw
        try:
            key = str(int(raw))
        except (TypeError, ValueError):
            key = str(raw)
        return names.get(key, str(raw))
    if vt in ("vec3", "vec"):
        return list(raw)
    return raw


def denormalize(desired, binding):
    v = binding["value"]
    vt = v["type"]
    if vt == "bool":
        return v.get("true", True) if desired else v.get("false", False)
    if vt == "color":
        return int(desired.lstrip("#"), 16)
    if vt == "enum":
        names = v.get("names", {})
        if not names:
            return desired
        for k, name in names.items():
            if name == desired:
                try:
                    return int(k)
                except ValueError:
                    return k
        raise ValueError(f"Enum value {desired!r} is not in names table")
    if vt in ("vec3", "vec"):
        return tuple(desired)
    return desired


def desired_of(args, binding):
    if binding["value"]["type"] == "vec3":
        return [args["x"], args["y"], args["z"]]
    return args["value"]

def get(args, binding, engine):
    raw = engine.get_raw(args, binding)
    return {"status": "ok",
            "target": engine.describe_target(args, binding),
            "property": binding["path"],
            "current": normalize(raw, binding)}


def run(args, binding, engine):
    desired = desired_of(args, binding)
    engine.set_raw(args, binding, denormalize(desired, binding))
    return {"status": "ok",
            "target": engine.describe_target(args, binding),
            "property": binding["path"],
            "value": desired}
