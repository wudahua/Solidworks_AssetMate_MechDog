"""Static guard for the registration layer (server.py).

Why this exists: version 0.4.6 shipped six tools that raised NameError on the first call —
`material` / `ledger` / `survey` were never imported, and one wrapper referenced `model_path`
that its signature did not declare. `py_compile` passed every time, because the syntax was
fine and the names simply did not exist.

Why it is narrow on purpose: a general undefined-name checker produced 125 false positives
on this codebase (closure captures, comprehension targets, nested scopes), and a check that
cries wolf gets ignored. This one only inspects what actually broke:

  1. inside every `@mcp.tool()` function, any `name.attribute` reference must resolve to an
     import, a parameter, or a module-level definition
  2. a one-line wrapper that forwards `kw=value` must have `kw` and `value` in scope

Dependency-free: run with the same Python that runs the server, no pyflakes needed.
"""

import ast
import os
import pathlib
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "solidworks_assetmate_mechdog", "server.py")
TOOLS_DIR = os.path.join(ROOT, "src", "solidworks_assetmate_mechdog", "tools")
README = os.path.join(ROOT, "README.md")
PYPROJECT = os.path.join(ROOT, "pyproject.toml")

# Members win32com dispatches as PROPERTYGET although they are methods. Calling them
# directly raises DISP_E_MEMBERNOTFOUND, which reads like "the API does not exist" — this
# cost two days on InsertGtol and hours twice more. They must go through
# sw_session.call_method(). Duplicated from sw_session so this check needs no pywin32.
PROPERTY_TRAPS = frozenset({
    "InsertGtol", "GetAnnotation", "GetAnnotation2", "FirstFeature", "GetNextFeature",
    "GetDefinition", "SetPosition2", "Select3", "SetScale",
})


def _module_scope(tree):
    imports, defined = set(), set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imports.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    defined.add(sub.id)
    return imports, defined


def _params(func):
    args = func.args
    names = set()
    for group in (args.posonlyargs, args.args, args.kwonlyargs):
        for arg in group:
            names.add(arg.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _locals(func):
    """Names bound anywhere inside the function (assignments, loops, with, comprehension)."""
    names = set()
    for sub in ast.walk(func):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            names.add(sub.id)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(sub.name)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            names.add(sub.name)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def check(path=SERVER):
    source = open(path, encoding="utf-8").read()
    tree = ast.parse(source, filename=path)
    imports, defined = _module_scope(tree)
    problems = []

    tools = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            is_tool = ((isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr == "tool")
                       or (isinstance(dec, ast.Attribute) and dec.attr == "tool"))
            if is_tool:
                tools.append(node)

    for func in tools:
        scope = imports | defined | _params(func) | _locals(func)
        for sub in ast.walk(func):
            # name.attribute — the module every tool forwards to
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                base = sub.value.id
                if base not in scope:
                    problems.append("%s:%d: %s() uses '%s.%s' but '%s' is not imported "
                                    "or defined"
                                    % (os.path.basename(path), getattr(sub, "lineno", 0),
                                       func.name, base, sub.attr, base))
            # wrapper forwarding a keyword it never received
            if isinstance(sub, ast.Call):
                for keyword in sub.keywords:
                    if keyword.arg is None:
                        continue
                    value = keyword.value
                    if isinstance(value, ast.Name) and value.id not in scope:
                        problems.append("%s:%d: %s() forwards '%s=%s' but '%s' is not in scope"
                                        % (os.path.basename(path), getattr(keyword, "lineno", 0),
                                           func.name, keyword.arg, value.id, value.id))
    return tools, problems


def check_tools_module(path):
    """Undefined module/module-level names inside a tools/ module.

    Narrower than a general F821 pass on purpose: only `name.attribute` references are
    checked, against module imports + module-level definitions + the function's own
    parameters/locals. That catches the `re` / `glob` class of bug (a module used but
    never imported) without drowning in closure and comprehension false positives.
    """
    source = open(path, encoding="utf-8").read()
    tree = ast.parse(source, filename=path)
    imports, defined = _module_scope(tree)
    module_scope = imports | defined
    problems = []
    for func in [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        scope = module_scope | _params(func) | _locals(func)
        for sub in ast.walk(func):
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                base = sub.value.id
                # Only flag names that look like modules we forgot to import: a single
                # lowercase word with dots would be noise, so require it to be a known
                # stdlib/local module prefix or an already-imported sibling.
                if base in scope:
                    continue
                if base in ("re", "os", "sys", "json", "glob", "time", "math", "shutil",
                            "tempfile", "subprocess", "pathlib", "collections"):
                    problems.append("%s:%d: %s() uses '%s.%s' but '%s' is not imported"
                                    % (os.path.basename(path), getattr(sub, "lineno", 0),
                                       func.name, base, sub.attr, base))
    return problems


def _duplicate_defs_in_body(body, scope, path):
    """Collect duplicate def names within one scope (module level or one class)."""
    seen, errors = {}, []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in seen:
                errors.append(
                    "%s: duplicate def %s%s (line %s and line %s) — the later one "
                    "silently wins, so the earlier code never runs"
                    % (os.path.basename(path), scope, node.name,
                       seen[node.name], node.lineno))
            else:
                seen[node.name] = node.lineno
        elif isinstance(node, ast.ClassDef):
            errors.extend(_duplicate_defs_in_body(node.body, scope + node.name + ".", path))
    return errors


def check_duplicate_defs(path):
    """Flag a second definition of the same name in the same scope.

    This is not caught by the interpreter, by import, or by most tests — the module simply
    binds the later object and the earlier one vanishes. It cost 40 minutes here once:
    `layout.py` ended up with two `add_bom_table` (new one mid-file, old macro-driven one at
    the end), so the tool kept running the old code while the standalone script — importing
    the same module — looked fine.
    """
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    except SyntaxError:
        return []  # the compile check reports syntax errors
    return _duplicate_defs_in_body(tree.body, "", path)


def check_stale_roadmap(readme_path, pyproject_path):
    """Flag a README roadmap entry still marked 待做 for a version <= current.

    The README keeps its own short roadmap next to a link to docs/ROADMAP.md. They drifted:
    README said "v0.5 待做" while ROADMAP said v0.5-v0.8 were all released, and the version
    counter had already passed 0.8. Someone reading the README would misjudge progress.
    """
    import re

    problems = []
    try:
        current = re.search(r'^version\s*=\s*"([0-9.]+)"',
                            pathlib.Path(pyproject_path).read_text(encoding="utf-8"),
                            re.MULTILINE)
        if not current:
            return problems
        cur = tuple(int(x) for x in current.group(1).split("."))
        text = pathlib.Path(readme_path).read_text(encoding="utf-8")
    except OSError:
        return problems

    for match in re.finditer(r"v(\d+)\.(\d+)[^\n]{0,40}待做", text):
        ver = (int(match.group(1)), int(match.group(2)))
        if ver <= cur:
            problems.append(
                "%s: roadmap says v%d.%d 待做 but the package is already %d.%d — "
                "either mark it done or move it forward"
                % (os.path.basename(readme_path), ver[0], ver[1], cur[0], cur[1]))
    return problems


def check_readme(tool_names):
    """README must agree with the code: tool counts and every tool name present.

    Two consecutive releases shipped without touching README — the headline feature of
    each release was missing from the docs, and the capability table still told readers a
    working feature was impossible. A doc that drifts is worse than no doc, so this runs
    with the same static check that catches broken imports.
    """
    readme_path = os.path.join(ROOT, "README.md")
    if not os.path.exists(readme_path):
        return ["README.md not found"]
    text = open(readme_path, encoding="utf-8").read()
    problems = []

    # Declared totals in the three section headers, e.g. "### 二、工程图（18）"
    import re

    declared = [int(n) for n in re.findall(r"^###\s*[一二三四五六七八九十]+、[^\n（]*（(\d+)）",
                                            text, re.MULTILINE)]
    if declared:
        total = sum(declared)
        if total != len(tool_names):
            problems.append("README section headers sum to %d tools but the code registers %d"
                            % (total, len(tool_names)))
    else:
        problems.append("README has no section headers declaring a tool count")

    missing = [name for name in tool_names if ("`%s`" % name) not in text]
    if missing:
        problems.append("README never mentions: %s" % ", ".join(missing))
    return problems


def check_property_traps(path, traps):
    """Fail on direct calls to members win32com dispatches as properties.

    `doc.InsertGtol()`, `note.GetAnnotation()`, `feat.FirstFeature()` all raise
    DISP_E_MEMBERNOTFOUND — which reads like "the API does not exist" and sent this project
    down a two-day dead end once, and cost hours twice more. They must go through
    `sw_session.call_method(obj, "Name", ...)`.

    Exempt: sw_session.py itself (where the helper lives) and any call already inside a
    helper wrapper.
    """
    if os.path.basename(path) == "sw_session.py":
        return []
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    except SyntaxError:
        return []
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in traps:
            continue
        # `call_method(obj, "FirstFeature")` passes the name as a string, not an attribute,
        # so it never reaches here. A bare `obj.FirstFeature()` does.
        problems.append(
            "%s:%d: `%s()` must go through sw_session.call_method(obj, \"%s\") — "
            "win32com dispatches it as a property and it raises MEMBERNOTFOUND"
            % (os.path.basename(path), getattr(node, "lineno", 0), func.attr, func.attr))
    return problems


def main():
    if not os.path.exists(SERVER):
        print("server.py not found at %s" % SERVER)
        return 1
    tools, problems = check()
    for name in sorted(os.listdir(TOOLS_DIR)) if os.path.isdir(TOOLS_DIR) else []:
        if name.endswith(".py"):
            module_path = os.path.join(TOOLS_DIR, name)
            problems.extend(check_tools_module(module_path))
            problems.extend(check_property_traps(module_path, PROPERTY_TRAPS))
            problems.extend(check_duplicate_defs(module_path))
    problems.extend(check_property_traps(SERVER, PROPERTY_TRAPS))
    problems.extend(check_duplicate_defs(SERVER))
    problems.extend(check_readme([t.name for t in tools]))
    problems.extend(check_stale_roadmap(README, PYPROJECT))
    if problems:
        print("static check FAILED (%d issue(s), %d tools scanned):" % (len(problems), len(tools)))
        for item in problems:
            print("  " + item)
        return 1
    print("static check OK (%d tools, every module reference resolves, README in sync)"
          % len(tools))
    return 0


if __name__ == "__main__":
    sys.exit(main())
