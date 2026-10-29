# AssetMate v0.6.1 安装与实测问题汇总

- **被测版本**：solidworks-assetmate-mechdog **0.6.1**（tag `v0.6.1`，commit `046e917`）
- **测试日期**：2026-10-28
- **测试环境**：Windows / SolidWorks `RevisionNumber 32.4.0`（32.x = 2025 版，SP4）/ Python 3.11.5 64-bit 虚拟环境 / pywin32 312 / mcp 1.30.0 / Pillow 13.0.0
- **安装方式**：`git clone` + `pip install -e ".[png]" --no-build-isolation`（PyPI 无此包，`/pypi/solidworks-assetmate-mechdog/json` 返回 404）
- **结论**：安装链路可用，出图闭环可跑通；**但「零件」在 2 个工具里被当成装配体打开，100% 失败**，且错误信息指向了错误的方向。这是本次的核心问题。

---

## 一、核心问题（P0）：`get_mass_properties` / `export_preview` 对零件必然失败

### 现象

对一个 `.SLDPRT` 调用这两个工具，返回结构化错误：

```json
{ "ok": false, "error": "mass_failed",
  "message": "SolidWorks could not open the document. It may be open read-only elsewhere, or another file with the same name is already open (see docs/com-quirks.md, section 2)." }
```

```json
{ "ok": false, "error": "preview_failed",
  "message": "SolidWorks could not open the document. It may be open read-only elsewhere, or another file with the same name is already open (see docs/com-quirks.md, section 2)." }
```

### 根因

`tools/assembly.py` 里这两个函数把文档类型写死为装配体：

```python
doc = open_document(assembly_path, SW_DOC_ASSEMBLY)   # mass_properties()
doc = open_document(assembly_path, SW_DOC_ASSEMBLY)   # export_preview()
```

对 `.SLDPRT` 传 `doc_type=2`（装配体），`OpenDoc6` 直接返回 `None`。

同目录 `tools/export.py` 已有正确写法，却没有被复用：

```python
SOURCE_TYPES = {".sldprt": SW_DOC_PART, ".sldasm": SW_DOC_ASSEMBLY}
doc = open_document(item["source"], SOURCE_TYPES[ext])
```

### 最小复现（脱离本包，纯 COM 验证）

同一零件、同一会话、连续两次 `OpenDoc6`：

| doc_type | 返回值 | errors | warnings |
|---|---|---|---|
| 2（装配体） | `None` | `2097152` | 0 |
| 1（零件） | `AM_TEST_01.SLDPRT` | `0` | 0 |

即：文件本身没问题、没有被占用、路径含中文与空格也不影响——**只是 doc_type 错了**。

### 二次伤害：错误信息把人带偏

`OpenDoc6` 的 `errors=2097152`、以及 `swSession.open_document` 里的 `errors/warnings` 被丢弃，最终只留一句"文件可能被占用"。使用者会去关文档、改只读、重建副本，实测本机为这条提示白白排查了两轮（第一次还怀疑是"同名零件已在 SolidWorks 中打开"，改了文件名依旧失败）。

### 修复建议

1. `mass_properties` / `export_preview` 改为按扩展名映射 doc_type（直接搬 `export.SOURCE_TYPES`，或下沉到 `sw_session.open_document` 内部按扩展名自动判断，`doc_type=None` 时自动推断）。
2. `open_document` 失败时把 `errors` / `warnings` 数值带进异常与返回体（例如 `"errors": 2097152`），并在消息里明确"doc_type 与文件类型不匹配"这一最常见分支。
3. 参数名同步：这两个工具形参叫 `assembly_path`，README 表格却写"整机质量"，使用者拿零件去试是必然路径。建议改 `model_path`（保留 `assembly_path` 作别名）。

---

## 二、其余问题

| # | 级别 | 问题 | 证据 | 建议 |
|---|---|---|---|---|
| 2 | P1 | `produce_drawing` 自己算出的 plan 没有被下游 dimensions 步骤消费 | 返回体 `plan.keep_dimensions=5`，实际 `annotations.dimension=15`，只在 `dimension_gap` 里写"规划按启发式筛选；实际导入的是模型全部尺寸" | 要么 dimensions 步骤真正只标 keep 列表，要么 `do_plan=false` 默认关闭，别让"规划"变成只写进返回体的装饰 |
| 3 | P1 | `list_drawing_annotations` 把标题栏模板静态文字全部计入 `note` | 一张刚生成、未加任何文字的 A3 图纸返回 `note: 82`，逐条内容是"审核/校核/设计/标记/处数…"，位置全在标题栏区 | 过滤 Figure/模板层文本，或给它们单独 `scope: title_block` 并从 `annotation_count` 里剔除；否则 README 里"真实图纸注释均值 46.5"的基线与 `drawing_audit`/`delivery_check` 的判定都会被模板噪声顶满 |
| 4 | P2 | 工具跑完不关闭文档，残留 `~$xxx.SLDDRW` 锁文件 | 本次测试期间 SW 会话文档数 3 → 6；输出目录残留 `~$AM_TEST_01.SLDDRW`，需手工 `CloseDoc` 才消失 | `produce_drawing` / `create_drawing` 结束关闭自己新建的工程图（或提供 `keep_open=false` 默认值）。com-quirks 已写"批量读图需逐张关闭"，工具层同样适用 |
| 5 | P2 | README 能力边界与 0.6.1 实际能力不一致 | README 仍写"形位公差写不进去 / 孔标注不可用 / material_library 失效"；而 `check_capabilities` 显示 gtol 已突破、`hole_callout` 为 pilot、本次实测 `callouts: created 8 / failed 0 / scanned 20` | 把 README 的"能力边界"表换成 `check_capabilities` 的口径，或直接删表指向工具输出 |
| 6 | P2 | README 工具数与实际不符 | README "35 个工具函数"，`static_check` 与 `tools/list` 均为 **39**；新增的 `produce_drawing` / `plan_drawing` / `check_capabilities` 三个工具在 README 的三个能力域表格里没有条目 | 补上三行，或表格改成从 `server.py` 自动生成 |
| 7 | P3 | 参数命名风格不统一 | 装配类 `assembly_path`、出图类 `model_path` / `drawing_path`；同一个 `.SLDPRT` 在不同工具里要换三个名字 | 统一为 `model_path`（装配体也吃），减少调用方的试错 |
| 8 | P3 | 文档多处写"SW2026 实测"，本机是 32.4.0（2025 SP4） | `check_capabilities` 的 `why` 字段多条写"SW2026 上不弹模态对话框"；README 写"开发验证 2024 SP4" | 能力表的 evidence 里写清版本号区间，避免使用者拿不同版本对不上结论 |
| 9 | P3 | 包未发布到 PyPI，README 的 `pip install .` 隐含"必须先 clone" | `https://pypi.org/pypi/solidworks-assetmate-mechdog/json` → 404 | README 安装章节第一步明确 `git clone`；或发个 sdist 到 PyPI/私有源 |
| 10 | P3 | `smoke.py` 裸跑直接 exit 2 | 默认路径指向未入库的 `测试用例/`，仓内不含模型 | 裸跑时打印"未找到默认模型，请用 `--part/--assembly` 指定"再退出，比 traceback 友好 |

---

## 三、通过项（供作者对照，说明不是环境问题）

| 检查 | 结果 |
|---|---|
| `tests/static_check.py` | PASS：39 tools, every module reference resolves |
| `check_capabilities` | PASS：7 项，blocked 2（dxf_import、geometry_creation），partial 2 |
| MCP 握手 / `tools/list` | PASS：stdio 正常，39 个工具全部可列 |
| `get_assembly_tree`（46 组件） | PASS：返回组件名/路径/颜色，中文路径正常 |
| `export_preview`（装配体 → PNG 1600×1200） | PASS：Pillow 链路正常，285 KB |
| `produce_drawing`（一般零件，A3） | PASS：10 步全 ok；`verdict 待设计审核`，`coverage 0.733`，`layout_warnings []`，`callouts 8/0/20` |
| `list_drawing_annotations` | 功能正常，计数口径见问题 3 |
| 中文路径 / 空格路径 | 无影响（`D:\WorkBuddy_space\MCP Solidworks_AssetMate_MechDog 安装与更新\...`） |

---

## 四、优先级建议

1. **先修问题 1**：两个工具改 doc_type 映射 + 回传 COM 错误码，改动 < 20 行，但直接影响"零件能不能用这套工具"的第一印象。
2. **问题 2、3** 属于"判定可信度"问题，与项目主打卖点（`delivery_check` 说得清能不能交）直接冲突，建议同批处理。
3. 问题 5、6 是文档债，改起来最快，建议随下一个 release 一并刷新。
