"""诊断：围绕「总列数 5 vs 可见列 3」这个核心矛盾，把能读到的状态全打印出来。

目的不是修复，是给规划者提供判断依据：
    TotalColumnCount = 5 但 ColumnCount = 3，到底是
      A) 模板定义列 vs 实际渲染列
      B) 隐藏列，但 ColumnHidden 索引错位
      C) ColumnHidden 是 1-based

用法：先在 01 脚本上跑通（能创建表格），再跑这个。或直接对已有的 BOM 图纸跑。
"""
import os
import sys

import pythoncom
import win32com.client as win


# ==== 改这里：指向一张已经插入过 BOM 的图纸 ====
DRAWING = r"D:\WorkBuddy_space\_assetmate_test_run\bom_test\BOM_TEST.SLDDRW"   # 宏插入成功的那张
# 或者指向 01 脚本生成的图纸，对照两者的差异
# ================================================


def call_method(obj, name, *args):
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    flag = getattr(obj, "_FlagAsMethod", None)
    if flag is not None:
        try:
            flag(name)
        except Exception:
            pass
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


def dump_table(tb, label):
    print("=" * 60)
    print(label)
    print("=" * 60)
    for attr in ("RowCount", "ColumnCount", "TotalColumnCount"):
        try:
            print("  %-18s %s" % (attr, getattr(tb, attr)))
        except Exception as exc:
            print("  %-18s ERR %s" % (attr, str(exc)[:40]))

    total = int(tb.TotalColumnCount)
    visible = int(tb.ColumnCount)
    print("  -> 差额: 总 %d / 可见 %d / 差 %d 列" % (total, visible, total - visible))

    print("\n  逐列（0 到 TotalColumnCount-1）:")
    ole = tb._oleobj_
    try:
        disp_hidden = ole.GetIDsOfNames("ColumnHidden")
    except Exception:
        disp_hidden = None

    for i in range(total):
        line = "    col%d" % i
        try:
            line += "  title=%-12s" % str(tb.GetColumnTitle2(i, True)).replace("<FONT size=3.5>", "")[:12]
        except Exception:
            line += "  title=?"
        if disp_hidden is not None:
            try:
                hidden = ole.Invoke(disp_hidden, 0, pythoncom.DISPATCH_PROPERTYGET, True, i)
                line += "  hidden=%s" % hidden
            except Exception:
                line += "  hidden=?"
        # 尝试读列类型（本机常报类型不匹配，能读则记录）
        try:
            ctype = call_method(tb, "GetColumnType3", i, True, None, None)
            line += "  type=%s" % str(ctype)[:20]
        except Exception:
            line += "  type=ERR"
        print(line)

    try:
        bf = tb.BomFeature
        print("\n  BomFeature:")
        for attr in ("TableType", "GetReferencedModelName", "KeepMissingItems",
                     "StrikeoutMissingItems", "Configuration"):
            try:
                print("    %-24s %s" % (attr, getattr(bf, attr)))
            except Exception as exc:
                print("    %-24s ERR %s" % (attr, str(exc)[:40]))
        try:
            print("    %-24s %s" % ("GetConfigurationCount",
                                    call_method(bf, "GetConfigurationCount")))
        except Exception as exc:
            print("    %-24s ERR %s" % ("GetConfigurationCount", str(exc)[:40]))
    except Exception as exc:
        print("  BomFeature ERR", str(exc)[:50])


def main():
    pythoncom.CoInitialize()
    sw = win.Dispatch("SldWorks.Application")

    if not os.path.exists(DRAWING):
        print("图纸不存在:", DRAWING)
        return 1

    doc = sw.OpenDoc6(DRAWING, 3, 1, "", 0, 0)
    if doc is None:
        print("打开失败（可能已被占用）")
        return 1
    sw.ActivateDoc3(os.path.basename(DRAWING), False, 0, 0) if False else None

    sheet = doc.GetFirstView
    tables = list(sheet.GetTableAnnotations or ())
    print("图纸上的表格数:", len(tables))
    if not tables:
        print("没有表格。先在界面上手工插入一个 BOM，或跑 01 脚本。")
        return 1

    for idx, raw in enumerate(tables):
        dump_table(win.Dispatch(raw), "表格 #%d" % idx)

    # 视图侧参考信息
    print("\n" + "=" * 60)
    print("视图信息")
    print("=" * 60)
    view = sheet.GetNextView
    while view is not None:
        try:
            t = int(view.Type)
        except Exception:
            t = None
        if t != 1:
            for attr in ("Name", "Type", "ReferencedConfiguration"):
                try:
                    print("  %-24s %s" % (attr, getattr(view, attr)))
                except Exception:
                    pass
            try:
                ref = call_method(view, "ReferencedDocument")
                if ref is not None:
                    print("  %-24s %s" % ("ReferencedDocument",
                                          str(win.Dispatch(ref).GetPathName)))
            except Exception:
                pass
            break
        view = view.GetNextView

    print()
    print("判读要点：")
    print("  1. 若这里也是 Total=5 / Visible=3，说明差异不在图纸，而在配置关联")
    print("  2. 若某个 colN 的 hidden=True，而 ColumnCount 不含它，则 B 或 C 成立")
    print("  3. 若所有 hidden 都是 False 但 ColumnCount 更小，则 A 成立（渲染列 vs 定义列）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
