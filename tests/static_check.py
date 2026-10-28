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
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "solidworks_assetmate_mechdog", "server.py")
TOOLS_DIR = os.path.join(ROOT, "src", "solidworks_assetmate_mechdog", "tools")


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


def main():
    if not os.path.exists(SERVER):
        print("server.py not found at %s" % SERVER)
        return 1
    tools, problems = check()
    for name in sorted(os.listdir(TOOLS_DIR)) if os.path.isdir(TOOLS_DIR) else []:
        if name.endswith(".py"):
            problems.extend(check_tools_module(os.path.join(TOOLS_DIR, name)))
    if problems:
        print("static check FAILED (%d issue(s), %d tools scanned):" % (len(problems), len(tools)))
        for item in problems:
            print("  " + item)
        return 1
    print("static check OK (%d tools, every module reference resolves)" % len(tools))
    return 0


if __name__ == "__main__":
    sys.exit(main())
