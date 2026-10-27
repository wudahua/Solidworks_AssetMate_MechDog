"""Survey a folder of real drawings and produce a statistical baseline.

Purpose: before changing any verdict threshold, measure what real drawings actually look
like. The numbers this reports are what `delivery_check` and `drawing_audit` are calibrated
against — running it again after a new batch of drawings arrives keeps those thresholds
honest.

Two operational facts learned the hard way:
  * SolidWorks refuses to open anything once ~40 documents are loaded, so each drawing is
    closed before the next one starts.
  * A single drawing takes 4-11 s to open + audit + close, so sampling is the only sane
    way to handle thousands.
"""

import glob
import json
import os
import random
import shutil
import statistics
import tempfile
import time

from ..sw_session import error, ok


def _percentiles(values):
    if not values:
        return None
    values = sorted(values)
    n = len(values)

    def at(p):
        return values[min(n - 1, max(0, int(round(p * (n - 1)))))]

    return {
        "count": n,
        "min": values[0],
        "p25": at(0.25),
        "p50": at(0.50),
        "p75": at(0.75),
        "max": values[-1],
        "mean": round(sum(values) / n, 1),
    }


def survey_drawings(folder, sample_size=100, seed=28, html_path=None, pattern="**/*.SLDDRW"):
    """Audit a random sample of drawings under `folder` and report the distribution.

    Returns counts for views/dimensions/notes/center marks plus the share of drawings
    carrying surface finish, geometric tolerance and balloons — the four numbers every
    verdict threshold should be based on.
    """
    from . import drawing as drawing_tools

    root = os.path.abspath(folder)
    if not os.path.isdir(root):
        return error("bad_input", "folder not found: %s" % root)

    pool = glob.glob(os.path.join(root, pattern), recursive=True)
    if not pool:
        return error("empty_pool", "no files matched %s under %s" % (pattern, root))

    rng = random.Random(seed)
    sample = rng.sample(pool, min(int(sample_size), len(pool)))

    try:
        from ..sw_session import get_sw

        sw = get_sw()
    except Exception:
        sw = None

    rows, failed = [], []
    started = time.time()
    for index, src in enumerate(sample, 1):
        work = tempfile.mkdtemp(prefix="survey_")
        target = os.path.join(work, "survey_target.SLDDRW")
        try:
            shutil.copy2(src, target)
        except Exception as exc:
            failed.append({"file": os.path.basename(src), "reason": "copy: %s" % str(exc)[:60]})
            shutil.rmtree(work, ignore_errors=True)
            continue
        began = time.time()
        audit = drawing_tools.drawing_audit(target)
        if not audit.get("ok"):
            failed.append({"file": os.path.basename(src),
                           "reason": "%s: %s" % (audit.get("error"),
                                                 (audit.get("message") or "")[:60])})
        else:
            annotations = drawing_tools.list_drawing_annotations(target)
            totals = annotations.get("totals") or {}
            rows.append({
                "file": os.path.basename(src),
                "folder": os.path.basename(os.path.dirname(src)),
                "views": audit.get("view_count"),
                "dimensions": audit.get("total_dimensions"),
                "notes": int(totals.get("note", 0)),
                "surface_finish": int(totals.get("surface_finish", 0)),
                "geometric_tolerance": int(totals.get("geometric_tolerance", 0)),
                "center_marks": int(totals.get("center_mark", 0)) + int(totals.get("centerline", 0)),
                "balloons": int(totals.get("balloon", 0)),
                "seconds": round(time.time() - began, 1),
                "warnings": [w.get("issue") for w in (audit.get("warnings") or [])],
            })
        # Closing is mandatory: after ~40 open documents SolidWorks stops opening files.
        if sw is not None:
            try:
                sw.CloseDoc("survey_target.SLDDRW")
            except Exception:
                pass
        shutil.rmtree(work, ignore_errors=True)

    elapsed = round(time.time() - started, 1)
    summary = {}
    for key in ("views", "dimensions", "notes", "center_marks", "seconds"):
        summary[key] = _percentiles([r[key] for r in rows if isinstance(r.get(key), int)])

    total = len(rows) or 1
    shares = {
        "with_surface_finish": sum(1 for r in rows if r["surface_finish"] > 0),
        "with_geometric_tolerance": sum(1 for r in rows if r["geometric_tolerance"] > 0),
        "with_balloons": sum(1 for r in rows if r["balloons"] > 0),
    }
    shares = {k: {"count": v, "ratio": round(v / total, 3)} for k, v in shares.items()}

    warning_counts = {}
    for row in rows:
        for issue in row.get("warnings") or []:
            warning_counts[issue] = warning_counts.get(issue, 0) + 1

    folders = {}
    for row in rows:
        folders[row["folder"]] = folders.get(row["folder"], 0) + 1

    result = ok(
        folder=root,
        pool=len(pool),
        sampled=len(sample),
        analysed=len(rows),
        failed_count=len(failed),
        elapsed_s=elapsed,
        per_drawing_s=round(elapsed / max(1, len(sample)), 1),
        summary=summary,
        shares=shares,
        warning_counts=dict(sorted(warning_counts.items(), key=lambda kv: -kv[1])),
        categories=dict(sorted(folders.items(), key=lambda kv: -kv[1])[:10]),
        failed=failed[:20],
        rows=rows,
        note="这些数字是 delivery_check / drawing_audit 阈值的依据；换一批图纸后应重跑。",
    )

    if html_path and rows:
        try:
            result["html"] = _write_html(result, html_path)
        except Exception as exc:
            result["html_error"] = str(exc)[:120]
    return result


def _write_html(result, html_path):
    """Render the survey as a standalone HTML page (inline CSS, no external assets)."""
    summary = result.get("summary") or {}
    shares = result.get("shares") or {}
    rows = result.get("rows") or []

    def cell(stat, key):
        if not stat:
            return "-"
        return str(stat.get(key, "-"))

    stat_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            name,
            cell(summary.get(key), "min"), cell(summary.get(key), "p25"),
            cell(summary.get(key), "p50"), cell(summary.get(key), "p75"),
            cell(summary.get(key), "max"), cell(summary.get(key), "mean"),
        )
        for name, key in (("视图", "views"), ("尺寸", "dimensions"), ("注释", "notes"),
                          ("中心标记", "center_marks"), ("耗时(s)", "seconds"))
    )

    share_rows = "".join(
        "<tr><td>%s</td><td>%d</td><td>%.0f%%</td></tr>" % (
            name, shares[key]["count"], shares[key]["ratio"] * 100,
        )
        for name, key in (("表面粗糙度", "with_surface_finish"),
                          ("形位公差", "with_geometric_tolerance"),
                          ("零件序号", "with_balloons"))
    )

    detail_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            row["file"][:42], row["folder"][:18], row["views"], row["dimensions"],
            row["geometric_tolerance"], row["surface_finish"], row["center_marks"],
        )
        for row in rows[:60]
    )

    warnings = result.get("warning_counts") or {}
    warn_html = "".join("<li>%s — %d 张</li>" % (k, v) for k, v in warnings.items()) or "<li>无</li>"

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>真实图纸画像</title>
<style>
body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;margin:0;padding:32px;background:#f7f9fc;color:#1f2733}
h1{font-size:24px;margin:0 0 6px}
.meta{color:#5f6b7a;font-size:13px;margin-bottom:24px}
.card{background:#fff;border:1px solid #e3e9f2;padding:20px 24px;margin-bottom:20px}
h2{font-size:16px;margin:0 0 14px;color:#2b3648}
table{border-collapse:collapse;width:100%%;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #eef2f8}
th{color:#5f6b7a;font-weight:600;font-size:12px}
tr:last-child td{border-bottom:none}
ul{margin:0;padding-left:20px;font-size:13px;line-height:1.8}
.note{font-size:12px;color:#7a8598;margin-top:10px}
</style></head><body>
<h1>真实图纸画像</h1>
<div class="meta">样本 %(analysed)d / 池 %(pool)d · 失败 %(failed)d · 单张 %(per)s s · 用时 %(elapsed)s s<br>
来源：%(folder)s</div>

<div class="card"><h2>分布</h2>
<table><tr><th>指标</th><th>最小</th><th>P25</th><th>中位</th><th>P75</th><th>最大</th><th>平均</th></tr>
%(stat_rows)s</table></div>

<div class="card"><h2>制造类标注的出现率</h2>
<table><tr><th>标注</th><th>图纸数</th><th>占比</th></tr>%(share_rows)s</table>
<div class="note">依据这组数据：粗糙度按常规项检查，形位公差按可选项。</div></div>

<div class="card"><h2>审计警告分布</h2><ul>%(warn_html)s</ul></div>

<div class="card"><h2>明细（前 60 张）</h2>
<table><tr><th>图纸</th><th>分类</th><th>视图</th><th>尺寸</th><th>形位</th><th>粗糙度</th><th>中心</th></tr>
%(detail_rows)s</table></div>
</body></html>""" % {
        "analysed": result.get("analysed", 0),
        "pool": result.get("pool", 0),
        "failed": result.get("failed_count", 0),
        "per": result.get("per_drawing_s"),
        "elapsed": result.get("elapsed_s"),
        "folder": result.get("folder"),
        "stat_rows": stat_rows,
        "share_rows": share_rows,
        "warn_html": warn_html,
        "detail_rows": detail_rows,
    }

    target = os.path.abspath(html_path)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(html)
    return target
