"""Material assignment — the missing half of BOM accuracy.

Without a material SolidWorks falls back to 1000 kg/m³, so every mass in a BOM is only
a volume proxy. `IPartDoc.SetMaterialPropertyName2(Config, Database, Name)` fixes that;
the database name and material name must both exist in the installed material library,
so a failed lookup is reported instead of silently keeping the old material.
"""

import glob
import os
import re

from ..sw_session import SW_DOC_PART, error, ok, open_document

DEFAULT_DATABASE = "SolidWorks Materials.sldmat"

# Keyword → material, used by assign_materials. Ordered: first match wins, so standard
# parts are listed before structural ones.
DEFAULT_RULES = [
    (("螺母", "螺钉", "螺栓", "螺柱", "垫圈", "销", "nut", "screw", "bolt", "washer", "pin"),
     "普通碳钢"),
    (("球", "球面", "ball"), "普通碳钢"),
    (("支架", "梁", "板", "座", "叉耳", "隔套", "bracket", "beam", "plate", "base"),
     "铝合金 6061"),
]


def _material_of(part):
    try:
        name = part.GetMaterialPropertyName2("", None)
        if name:
            return str(name)
    except Exception:
        pass
    try:
        raw = str(part.MaterialIdName or "")
        # MaterialIdName looks like "solidworks materials|普通碳钢|9"
        if "|" in raw:
            return raw.split("|")[1]
        return raw or None
    except Exception:
        return None


def _mass_kg(part):
    try:
        mp = part.Extension.CreateMassProperty()
        if mp is not None:
            return round(float(mp.Mass), 6)
    except Exception:
        pass
    return None


def set_part_material(paths, material, database=DEFAULT_DATABASE, config="",
                      save=True, apply=False):
    """Assign a material to one or more parts.

    Returns the previous material and mass for each part so the change is reversible and
    its effect on mass is visible. apply=False is a dry run.
    """
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        return error("bad_input", "No part paths given")

    results, changed, missing = [], [], []
    for path in paths:
        path = os.path.abspath(path)
        if not os.path.exists(path):
            missing.append({"path": path, "reason": "file_not_found"})
            continue
        if not path.lower().endswith(".sldprt"):
            missing.append({"path": path, "reason": "not_a_part"})
            continue
        try:
            part = open_document(path, SW_DOC_PART)
        except Exception as exc:
            missing.append({"path": path, "reason": str(exc)[:80]})
            continue
        before = _material_of(part)
        mass_before = _mass_kg(part)
        entry = {
            "part": os.path.basename(path),
            "material_before": before,
            "mass_before_kg": mass_before,
            "requested": material,
        }
        if not apply:
            entry["would_change"] = before != material
            results.append(entry)
            continue
        try:
            part.SetMaterialPropertyName2(config, database, material)
            try:
                part.EditRebuild3
            except Exception:
                pass
            try:
                part.SaveSilent
            except Exception:
                pass
        except Exception as exc:
            entry["error"] = str(exc)[:120]
            results.append(entry)
            continue
        after = _material_of(part)
        entry["material_after"] = after
        entry["mass_after_kg"] = _mass_kg(part)
        entry["ok"] = bool(after) and after == material
        if not entry["ok"]:
            entry["hint"] = ("名称或数据库在这台机器的材质库里不存在，"
                             "用 material_library 查看可选名称")
        else:
            changed.append(entry["part"])
        results.append(entry)

    return ok(
        material=material,
        database=database,
        applied=bool(apply),
        count=len(results),
        changed=changed,
        results=results,
        missing=missing,
    )


def _normalise_rules(rules):
    """Accept [{"keywords": [...], "material": "..."}] as well as [(keywords, material)].

    The MCP schema advertises `rules` as an array of objects, but the implementation used
    to unpack each element as a 2-tuple — a dict then yielded its KEYS, so the material
    became the literal string "material" (reported as 2.2).
    """
    if not rules:
        return None
    normalised = []
    for rule in rules:
        if isinstance(rule, dict):
            keywords = rule.get("keywords") or rule.get("keyword") or []
            material = rule.get("material")
        elif isinstance(rule, (list, tuple)) and len(rule) == 2:
            keywords, material = rule
        else:
            raise ValueError("each rule must be {keywords, material} or a 2-item list, "
                             "got %r" % (rule,))
        if isinstance(keywords, str):
            keywords = [keywords]
        if not material or not keywords:
            raise ValueError("rule needs both keywords and material: %r" % (rule,))
        normalised.append((list(keywords), str(material)))
    return normalised


def assign_materials(paths, rules=None, database=DEFAULT_DATABASE, config="",
                     save=True, apply=False):
    """Pick a material per part from keyword rules, then assign it.

    rules is a list of (keywords, material); the first rule whose keyword appears in the
    part name wins. Rules are advisory — the chosen material and the reason are reported
    for every part so a wrong guess is visible, and ai_supplied-style transparency is
    preserved in the delivery record.
    """
    if isinstance(paths, str):
        paths = [paths]
    try:
        rule_set = _normalise_rules(rules) or DEFAULT_RULES
    except ValueError as exc:
        return error("bad_rules", str(exc),
                     'Use [{"keywords": ["KSHE"], "material": "自定义塑料"}]')
    plan, unresolved = [], []
    for path in paths:
        path = os.path.abspath(path)
        name = os.path.basename(path)
        chosen, reason = None, None
        for keywords, material in rule_set:
            hit = next((k for k in keywords if k.lower() in name.lower()), None)
            if hit:
                chosen, reason = material, "名称含「%s」" % hit
                break
        if chosen is None:
            chosen, reason = "普通碳钢", "无规则命中，取默认值"
            unresolved.append(name)
        plan.append({"path": path, "part": name, "material": chosen, "reason": reason})

    by_material = {}
    for row in plan:
        by_material.setdefault(row["material"], []).append(row["path"])

    results = {}
    for material, group in by_material.items():
        results[material] = set_part_material(
            group, material, database=database, config=config, save=save, apply=apply,
        )

    return ok(
        applied=bool(apply),
        parts=len(plan),
        plan=plan,
        defaulted=unresolved,
        by_material={k: len(v) for k, v in by_material.items()},
        results=results,
        note="material 由名称关键词推断，属于「AI 拟定」值，交付时应作为待确认项列出。",
    )


def material_library(database=DEFAULT_DATABASE):
    """List material names available in a material library.

    `database` may be a bare filename (searched under the usual install roots), a
    directory, or an absolute path to a .sldmat. Chinese installs store the library as
    UTF-16, and the old utf-8 reader silently produced zero materials while reporting
    ok=true — both are handled here.
    """
    candidates = []
    if os.path.isfile(database):
        candidates = [database]
    elif os.path.isdir(database):
        candidates = sorted(glob.glob(os.path.join(database, "**", "*.sldmat"), recursive=True))
    else:
        roots = [
            os.environ.get("PROGRAMDATA", ""),
            os.environ.get("APPDATA", ""),
            os.path.join(os.environ.get("PROGRAMFILES", ""), "SOLIDWORKS Corp",
                         "SOLIDWORKS", "lang"),
        ]
        for root in roots:
            if not root or not os.path.isdir(root):
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for filename in filenames:
                    if filename.lower() == database.lower():
                        candidates.append(os.path.join(dirpath, filename))

    if not candidates:
        available = []
        for root in (os.environ.get("PROGRAMDATA", ""), os.environ.get("APPDATA", ""),
                     os.path.join(os.environ.get("PROGRAMFILES", ""), "SOLIDWORKS Corp")):
            if root and os.path.isdir(root):
                available += glob.glob(os.path.join(root, "**", "*.sldmat"), recursive=True)
        return error("library_not_found",
                     "No material library matching %r" % database,
                     "Available: %s" % (available[:6] or "none found"))

    found = []
    for file_path in candidates[:1]:
        text = None
        # UTF-16 first: Chinese SolidWorks ships UTF-16 libraries whose every byte is
        # followed by \x00, so a utf-8 read matches nothing.
        for encoding in ("utf-16", "utf-8-sig", "utf-8", "gb18030"):
            try:
                probe = open(file_path, "r", encoding=encoding, errors="strict").read()
                if "<material" in probe:
                    text = probe
                    break
            except Exception:
                continue
        if text is None:
            try:
                text = open(file_path, "r", encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
        for match in re.finditer(r'<material name="([^"]+)"', text):
            found.append(match.group(1))

    found = sorted(set(found))
    result = ok(database=database, path=candidates[0], count=len(found), materials=found)
    if not found:
        result["warning"] = ("parsed 0 materials from %s — file exists but no <material> "
                             "entries were readable" % candidates[0])
    return result
