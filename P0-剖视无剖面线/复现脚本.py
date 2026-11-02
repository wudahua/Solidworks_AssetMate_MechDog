"""P0 复现脚本：剖视图不生成剖面线。

用法（在仓库根目录执行）：

    python "P0-剖视无剖面线/复现脚本.py"

可选：传一个零件路径，默认用 飞达动力轴。

脚本会：
  1. 复制零件到临时目录（不改仓库里的模型）
  2. 建 A3 图纸 + front 视图
  3. 建一个 A-A 水平剖视
  4. 导出 PDF、渲染 PNG
  5. 打印剖视的 edge_count / sanity / 显示模式

然后请**打开生成的 PNG 看剖切面**：有没有 45° 剖面线。

要点：脚本用 try/finally 关文档。SolidWorks 被留下未关闭的文档后再打开同名文件会
报 OpenDoc6 65536，这个坑已经踩过三次。
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from solidworks_assetmate_mechdog.tools import drawing  # noqa: E402

DEFAULT_PART = os.path.join("测试用例", "飞达动力轴", "飞达动力轴.SLDPRT")


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PART
    if not os.path.exists(src):
        print("零件不存在：%s" % src)
        return 2

    work = tempfile.mkdtemp(prefix="p0_hatch_")
    part = os.path.join(work, "model.SLDPRT")
    drw = os.path.join(work, "drawing.SLDDRW")
    shutil.copy2(src, part)
    print("工作目录：%s" % work)

    try:
        created = drawing.create_drawing(part, output_path=drw, views=["front"], paper="A3")
        print("create_drawing ok=%s scale=%s" % (created.get("ok"), created.get("chosen_scale")))

        section = drawing.add_section_view(
            drw, view_name="工程图视图1", orientation="horizontal", label="A"
        )
        print(
            "add_section_view ok=%s edge_count=%s sanity=%s"
            % (
                section.get("ok"),
                section.get("edge_count"),
                (section.get("sanity") or {}).get("verdict"),
            )
        )

        # 显示模式：确认不是"线框"导致的空白
        doc = drawing._open_drawing(drw)
        sec_views = [v for v in drawing._iter_views(doc) if getattr(v, "Type", None) == 2]
        for view in sec_views:
            try:
                print("剖视 %s  GetDisplayMode2=%s" % (view.Name, int(view.GetDisplayMode2())))
            except Exception as exc:  # noqa: BLE001
                print("剖视 %s  显示模式读取失败：%s" % (view.Name, str(exc)[:60]))
            for probe in ("GetSection", "GetSectionLineInfo2"):
                try:
                    value = getattr(view, probe)
                    value = value() if callable(value) else value
                    print("  %s -> %r (len=%s)" % (probe, type(value).__name__, len(value or ())))
                except Exception as exc:  # noqa: BLE001
                    print("  %s 失败：%s" % (probe, str(exc)[:70]))

        pdf = os.path.join(work, "drawing.pdf")
        exported = drawing.export_drawing(drw, pdf)
        print("export ok=%s" % exported.get("ok"))
        if exported.get("ok"):
            rendered = drawing.render_drawing(pdf, output_dir=work, scale=2.0)
            pages = rendered.get("pages") or []
            for page in pages:
                print("PNG: %s" % page.get("path"))
    finally:
        drawing.close_document(drw)
        drawing.close_document(part)
        print("已关闭两个文档")

    print("\n下一步：打开上面的 PNG，看 A-A 剖切面有没有 45° 斜线。")
    print("若没有 —— 这就是 P0；请把 PNG 一并反馈。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
