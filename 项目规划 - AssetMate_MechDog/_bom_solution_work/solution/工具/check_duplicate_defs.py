"""Detect duplicate function definitions in the same module/class scope."""
import ast
from pathlib import Path


def _duplicate_defs_in_body(body, scope, path):
    seen = {}
    errors = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in seen:
                errors.append(
                    f"{path}: duplicate def {scope}{node.name} "
                    f"(line {seen[node.name]} and {node.lineno})"
                )
            else:
                seen[node.name] = node.lineno
        elif isinstance(node, ast.ClassDef):
            errors.extend(_duplicate_defs_in_body(node.body, f"{scope}{node.name}.", path))
    return errors


def check_duplicate_defs(path):
    path = Path(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _duplicate_defs_in_body(tree.body, "", path)


if __name__ == "__main__":
    import sys
    failures = []
    for filename in sys.argv[1:]:
        failures.extend(check_duplicate_defs(filename))
    print("\n".join(failures) if failures else "No duplicate definitions found.")
    raise SystemExit(1 if failures else 0)
