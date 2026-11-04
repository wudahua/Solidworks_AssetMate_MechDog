"""C group: the save-path corner cases the acceptance matrix lists but nobody exercised.

Run:  python tests/test_save_paths.py

Matrix section 12, "保存" row: 目标已打开 / 只读 / 同名冲突 / 路径未更新.

The question each case answers is NOT "does it work" — it is **does it fail loudly**.
A save that silently does nothing is worse than one that raises, because the caller gets
`ok: true` and the file on disk is the old one. That failure mode already happened once in
this project (v0.8.114: a failing save still returned ok:true), so these cases check the
RECEIPT as much as they check the file.

Each case is independent: a fresh temp dir, a fresh copy, no shared state except the
SolidWorks instance. Everything created here is closed at the end.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from solidworks_assetmate_mechdog.sw_session import get_sw              # noqa: E402
from solidworks_assetmate_mechdog.tools import drawing, pipeline        # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                   "测试用例", "旋转轴-工程图", "旋转轴.SLDPRT")

results = []


def record(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%-52s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    return ok


def fresh(tag=""):
    work = tempfile.mkdtemp(prefix="sav_%s_" % tag)
    token = uuid.uuid4().hex[:6]
    part = os.path.join(work, "m_%s.SLDPRT" % token)
    drw = os.path.join(work, "d_%s.SLDDRW" % token)
    shutil.copy2(SRC, part)
    return work, part, drw


def make_readonly(path):
    os.chmod(path, stat.S_IREAD)


def make_writable(path):
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass


def case_readonly_target():
    """A read-only .SLDDRW on disk: the run must not claim success and leave it untouched."""
    work, part, drw = fresh("ro")
    try:
        first = pipeline.produce_drawing(part, output_path=drw)
        if not first.get("ok"):
            return record("[readonly] setup", False, "the baseline run failed")
        size_before = os.stat(drw).st_size
        make_readonly(drw)
        try:
            second = pipeline.produce_drawing(part, output_path=drw, overwrite=True)
        finally:
            make_writable(drw)
        size_after = os.stat(drw).st_size
        # Either it refused outright, or it saved. What it must NOT do is say ok while the
        # file is unchanged.
        wrote = size_after != size_before
        honest = (not second.get("ok")) or wrote
        record("[readonly] no silent no-op", honest,
               "ok=%s size %d->%d" % (second.get("ok"), size_before, size_after))
        if not second.get("ok"):
            record("[readonly] error is named", bool(second.get("error")),
                   "error=%s" % second.get("error"))
        return None
    finally:
        drawing.close_document(drw)
        drawing.close_document(part)
        shutil.rmtree(work, ignore_errors=True)


def case_overwrite_guard():
    """output_path already exists and overwrite=False: must refuse, and keep the old file."""
    work, part, drw = fresh("ow")
    try:
        first = pipeline.produce_drawing(part, output_path=drw)
        if not first.get("ok"):
            return record("[overwrite] setup", False, "the baseline run failed")
        stamp = os.stat(drw).st_mtime_ns
        size = os.stat(drw).st_size
        second = pipeline.produce_drawing(part, output_path=drw, overwrite=False)
        unchanged = (os.stat(drw).st_mtime_ns == stamp and os.stat(drw).st_size == size)
        record("[overwrite] refuses without the flag",
               (not second.get("ok")) and unchanged,
               "ok=%s error=%s unchanged=%s" % (second.get("ok"), second.get("error"), unchanged))
        # And with the flag it must actually replace it.
        third = pipeline.produce_drawing(part, output_path=drw, overwrite=True)
        record("[overwrite] replaces with the flag", bool(third.get("ok")),
               "ok=%s error=%s" % (third.get("ok"), third.get("error")))
        return None
    finally:
        drawing.close_document(drw)
        drawing.close_document(part)
        shutil.rmtree(work, ignore_errors=True)


def case_same_name_conflict():
    """Two different files with the SAME NAME open at once — quirks section 2.

    SolidWorks refuses to open the second one. The failure must name the reason, not come
    back as a bare "could not open".
    """
    work_a, part_a, drw_a = fresh("sn1")
    work_b = tempfile.mkdtemp(prefix="sav_sn2_")
    part_b = os.path.join(work_b, os.path.basename(part_a))     # deliberately the same name
    try:
        shutil.copy2(SRC, part_b)
        first = pipeline.produce_drawing(part_a, output_path=drw_a)
        if not first.get("ok"):
            return record("[same name] setup", False, "the baseline run failed")
        # part_a is open (produce_drawing opened it and did not close it). Opening
        # part_b — same basename, different folder — is the conflict.
        second = pipeline.produce_drawing(part_b, output_path=os.path.join(work_b, "d.SLDDRW"))
        record("[same name] second open does not silently succeed",
               (not second.get("ok")) or bool(second.get("drawing")),
               "ok=%s error=%s msg=%s" % (second.get("ok"), second.get("error"),
                                          (second.get("message") or "")[:60]))
        if not second.get("ok"):
            msg = (second.get("message") or "") + " " + str(second.get("error") or "")
            record("[same name] message explains the conflict",
                   any(k in msg for k in ("same name", "同名", "already open", "已打开",
                                          "锁", "lock", "read-only", "只读")),
                   msg.strip()[:90])
        return None
    finally:
        drawing.close_document(drw_a)
        drawing.close_document(part_a)
        drawing.close_document(part_b)
        shutil.rmtree(work_a, ignore_errors=True)
        shutil.rmtree(work_b, ignore_errors=True)


def case_output_dir_missing():
    """A non-existent output folder: must create it or say why, never write nowhere."""
    work, part, _ = fresh("dir")
    drw = os.path.join(work, "nested", "deeper", "out.SLDDRW")
    try:
        payload = pipeline.produce_drawing(part, output_path=drw)
        exists = os.path.exists(drw)
        record("[missing dir] either writes the file or refuses",
               bool(payload.get("ok")) == exists,
               "ok=%s exists=%s error=%s" % (payload.get("ok"), exists, payload.get("error")))
        return None
    finally:
        drawing.close_document(drw)
        drawing.close_document(part)
        shutil.rmtree(work, ignore_errors=True)


def main():
    sw = get_sw()
    print("SolidWorks %s | docs %d" % (sw.RevisionNumber,
                                       len(list(sw.GetDocuments or ()))))
    print("-" * 84)
    for fn in (case_overwrite_guard, case_readonly_target, case_output_dir_missing,
               case_same_name_conflict):
        try:
            fn()
        except Exception as exc:                                   # noqa: BLE001
            record("[%s] raised" % fn.__name__, False,
                   "%s: %s" % (type(exc).__name__, str(exc)[:90]))
    failed = [r for r in results if not r[1]]
    print("-" * 84)
    print("%d passed / %d total" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
