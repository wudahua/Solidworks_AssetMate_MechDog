import io

# 1) bump both version strings
for path, old, new in (
        ("pyproject.toml", 'version = "0.8.133"', 'version = "0.8.137"'),
        ("src/solidworks_assetmate_mechdog/__init__.py",
         '__version__ = "0.8.133"', '__version__ = "0.8.137"')):
    s = io.open(path, encoding="utf-8").read()
    assert old in s, "version anchor missing in %s" % path
    s = s.replace(old, new, 1)
    io.open(path, "w", encoding="utf-8", newline="").write(s)

# 2) regression: the two version strings must agree, and must agree with the tag
p = "tests/regressions.py"
s = io.open(p, encoding="utf-8").read()

block = '''def assert_version_strings_agree():
    """0.8.137 — the package reported 0.8.133 while the tag was v0.8.136.

    Report 2026-11-05 (P1): three consecutive releases (0.8.134 / 0.8.135 / 0.8.136) were
    committed and tagged WITHOUT bumping the version, so `health_check` answered
    `package_version: 0.8.133` from a tree that contained 0.8.136 code. The self-identifying
    fields added four rounds earlier (package_path / package_version / python_executable)
    exist precisely because several install directories can coexist — and they were pointing
    at the wrong one.

    `static_check` passed throughout because nothing compared the two strings. This does,
    and it also compares against the git tag when one exists.
    """
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pyproject = io.open(os.path.join(root, "pyproject.toml"), encoding="utf-8").read()
    m = re.search(r'^version\\s*=\\s*"([^"]+)"', pyproject, re.M)
    assert m, "pyproject has no version line"
    declared = m.group(1)

    init = io.open(os.path.join(root, "src", "solidworks_assetmate_mechdog", "__init__.py"),
                   encoding="utf-8").read()
    m2 = re.search(r'__version__\\s*=\\s*"([^"]+)"', init)
    assert m2, "__init__.py has no __version__"
    declared_dunder = m2.group(1)

    assert declared == declared_dunder, (
        "pyproject says %r but __version__ says %r" % (declared, declared_dunder))

    # Compare with the tag only when HEAD is exactly on one: a commit made between releases
    # legitimately sits ahead of the last tag, and failing on that would train people to skip
    # this check.
    tag_version = None
    try:
        import subprocess
        out = subprocess.run(["git", "describe", "--tags", "--exact-match", "HEAD"],
                             cwd=root, capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            tag_version = out.stdout.strip().lstrip("v")
    except Exception:
        tag_version = None

    if tag_version:
        assert tag_version == declared, (
            "HEAD is tagged v%s but the package declares %r — bump before tagging"
            % (tag_version, declared))
    return "version %s agrees across pyproject, __init__ and tag%s" % (
        declared, (" v" + tag_version) if tag_version else " (untagged HEAD)")
'''
anchor = "STATIC_REGRESSIONS = ["
assert anchor in s and "def assert_version_strings_agree" not in s
s = s.replace(anchor, block + anchor, 1)

anchor2 = "assert_p3_receipts_say_what_they_mean),"
idx = s.find(anchor2)
ins = idx + len(anchor2)
if "0.8.137 version agree" not in s:
    s = s[:ins] + '\n    ("0.8.137 version strings agree", assert_version_strings_agree),' + s[ins:]

# io/os might not be imported at module level in regressions.py; add if missing
head = s[:2000]
if "\nimport io" not in head:
    s = s.replace("import ast", "import ast\nimport io\nimport os", 1)

io.open(p, "w", encoding="utf-8", newline="").write(s)
print("version bumped to 0.8.137 and guarded")
