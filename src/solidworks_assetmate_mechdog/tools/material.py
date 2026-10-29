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
    """Mass in kg, or None with a reason. Returning a bare None made `mass_after_kg: null`
    indistinguishable from "mass was zero" or "tool did not try"."""
    try:
        # CreateMassProperty is another member that pywin32 dispatches as a property and
        # then fails to find; call it through DISPATCH_METHOD like the rest.
        from ..sw_session import call_method
        mp = call_method(part.Extension, "CreateMassProperty")
    except Exception as exc:
        return None, "CreateMassProperty failed: %s" % str(exc)[:60]
    if mp is None:
        return None, "CreateMassProperty returned None (not available in this environment)"
    try:
        import win32com.client as _win
        value = float(_win.Dispatch(mp).Mass)
    except Exception as exc:
        return None, "Mass unreadable: %s" % str(exc)[:60]
    if value <= 0:
        return None, "Mass reported as %r — density likely missing on the assigned material" % value
    return round(value, 6), None


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
        mass_before, mass_before_note = _mass_kg(part)
        entry = {
            "part": os.path.basename(path),
            "material_before": before,
            "mass_before_kg": mass_before,
            "mass_before_note": mass_before_note,
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
        mass_value, mass_note = _mass_kg(part)
        entry["mass_after_kg"] = mass_value
        if mass_note:
            entry["mass_note"] = mass_note
        entry["ok"] = bool(after) and after == material
        if not entry["ok"]:
            # The old hint blamed a missing name, but the name may well exist (it lists
            # fine in material_library) — the real cause is unknown. Report what actually
            # happened: what the setter was told, and what the part reads back.
            entry["hint"] = ("SetMaterialPropertyName2(db=%r, name=%r) 已执行，"
                             "但回读仍是 %r。名称可能存在于库里（material_library 能列出）"
                             "而写入未生效——该节点可能带自定义属性导致 marshalling 失败。"
                             "可用 material_library 换库/换名重试。"
                             % (database, material, after))
        else:
            changed.append(entry["part"])
        results.append(entry)

    # Top-level `ok` must not be unconditional. It used to be, so a run where every part
    # failed still answered ok:true / applied:true — a caller reading only the top level
    # believed the material was set. Dry-run is the one case where "applied" is honestly
    # false by design, so it is reported separately instead of faking success.
    succeeded = [r for r in results if r.get("ok")]
    failed = len(results) - len(succeeded)
    payload = dict(
        material=material,
        database=database,
        applied=(bool(apply) and bool(succeeded)),
        dry_run=(not apply),
        succeeded=len(succeeded),
        failed=failed,
        count=len(results),
        changed=changed,
        results=results,
        missing=missing,
    )
    # Dry-run never writes, so "failed" is meaningless there — only judge when applying.
    if apply and failed:
        return error(
            "material_partial" if succeeded else "material_not_applied",
            "%d/%d 零件的材质未写成（成功 %d）" % (failed, len(results), len(succeeded)),
            "顶层 ok=false 是因为明细里有失败项——旧版本这里恒为 ok=true，"
            "让调用方误以为全部成功。逐个看 results[].hint 与环境信息。",
            **payload)
    return ok(**payload)


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
