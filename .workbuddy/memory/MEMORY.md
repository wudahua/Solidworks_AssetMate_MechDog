# 项目长期约定

> 详细踩坑史见 `docs/com-quirks.md` 与 `.workbuddy/memory/YYYY-MM-DD.md`；本文件只留跨会话必须遵守的规则。

## 仓库与结构

- `Solidworks_AssetMate_MechDog`：SolidWorks 工具集 MCP。GitHub 仓库名 = MCP 注册名（大小写一致），账号 `wudahua`，PUBLIC，分支 main。
- 工作区根即仓库根；`src/solidworks_assetmate_mechdog/`（工具在 `tools/`，COM 统一走 `sw_session.py`，MCP 外壳在 `server.py`）。
- 依赖：`mcp>=1.2,<2`（FastMCP）；Windows + pywin32；Python 包名小写。MCP 客户端读的是 **`server.py` 的 docstring**，改 `pipeline.py` 的注释对调用方不可见。
- 工具规范：全部返回 JSON；写操作默认 dry-run、执行前快照原值；慢 COM 只取一次；COM 不可用不得编造结果。
- 发布流程：`python tools/bump_version.py --to X.Y.Z` → `check_all` → `git commit` + `git tag` + push + `gh release create`。推送偶发代理 502（`CONNECT tunnel failed`），**只重试、不 `--force`**；`api.github.com` 常通而 `github.com:443` 不通。
- `用户反馈问题/`、`recolor-GitHub-交接/` 是本地资料，不发布、已 gitignore。

## 门禁与测试

- `python tests/check_all.py`（秒级，无 SolidWorks）：语法 + `static_check` + `regressions`。
- `python tests/regressions.py`：断言分**两个列表**——`STATIC_REGRESSIONS`（只读源码文本）与 `BEHAVIOURAL_REGRESSIONS`（import 包并引用它，能因逻辑错误失败）；`ALL_REGRESSIONS` 是并集。**所有覆盖数字都取 `len()`**，尾部打 `counts: total=… behavioural=… text_ast=…`，`check_all` 只解析这一行。禁止再看 docstring 标记或另写计数器（v0.8.327 曾出现 5/6/15 三个数字）。守卫 `assert_the_coverage_numbers_come_from_the_lists` 用 AST 校验分类，新增行为断言必须放进 beha档列表。
- `python tests/stdio_contract.py`：真 stdio 契约（不需 SolidWorks，故意不进 `check_all`）。
- `python tests/smoke.py`：真机行为层（`--only '<name>'` 单跑，`--part`/`--assembly` 指夹具）。夹具：`测试用例/接槽.SLDPRT`（有驱动尺寸，公差/尺寸类用例用它）；`P05_karetku` 是导入实体，无驱动尺寸 → 相关用例必须 `FixtureMissing` SKIP，不许假绿。全量约 26–69 min，长任务前台跑 + `timeout=600000` 自动转后台。
- 改完签名必须同步 smoke 与 `regressions` 里的签名断言；**改完必须跑一次真实调用再发布**。
- 断言分层原则：AST 断言看不见逻辑错误（`UnboundLocalError`、顺序错误、死代码）。凡涉及两个纯函数之间的行为，必须加**行为断言**或 smoke 注入用例（`mock.patch.object(..., side_effect=...)` 真调用再改写返回）。旧断言会反向锁死修复——改判据时必须回头枚举以旧判据为守卫的断言。

## 判据设计通则（本项目已因此返工多次）

- **一个事实只能有一个判据**：`ok` / `completeness` / `verdict` / `blocking` 分工固定，新增维度要回查所有基于旧判据的字段。`ok` 曾被三处当三种意思用（全成了吗 / 还值得继续吗 / 能用吗），动一处炸两处。
- **`0` 有三种含义**：没有对象、没有尝试、没有权限。被 `except` 吞掉的分支必须有「结果不可能为 0」的断言；没数过就报 `null` + `status:"not_checked"`，不许报 0。
- **置信度阶梯不许以裸 `else` 收尾**：0 次匹配会继承最高档结论（「唯一匹配/high」）。
- **标记要贯穿整条链路**：`blocking:false` 只喂第一个读它的地方等于没写；把「严不严重」抽成纯函数供首尾共用。
- **证据要分级，不要降级**：一档一档判（符号 > 裸数值 > 整页），把弱证据并进强证据的 `elif` 会让它继承强结论；反之全判不确定会把正确用例判死。
- **`null` 必须能回答「为什么是 null」**：读错对象 / 没 dispatch / API 边界要分开写进回执（`*_reason`）。
- **「建议」与「已完成」用不同字段**：`applied: true` 比措辞可靠。
- 过滤器一律白名单（黑名单永远落后于下一次新增的步骤）。

## COM / SolidWorks 硬口径

- **视图 outline**：`IView.GetOutline` 宽高各恒定虚胖 `OUTLINE_MARGIN_M = 0.0119`（与比例无关）。一切 overlap / 出界 / 打包用 `drawing.precise_outline()`（O(1) 零 COM）；`ink_outline()` 已删——四条取点路全断（`GetVisibleEntities2` 返回**轮廓边**、`GetStartVertex` 恒 `None`、`IBody2.GetVertices` 对全圆边返 None、`ICurve` 无 `GetEndPoint`）。圆边只能用 `ICurve.CircleParams`。
- 模型坐标→视图坐标**没有可用 API**（`GetViewPlaneNormal` 不存在、`ModelToViewTransform` 不是行主序 4x4）。只能用「视图 deflated 宽高 vs 包围盒三轴 extent」尺寸匹配，误差相同（正立方体）返回 `axes_ambiguous` **拒绝而不是猜**。
- 标注类型：`IAnnotation.GetType` 是**属性**（`int(GetType())` 必抛 TypeError）；尺寸 4、note 6；`IDisplayDimension.GetType` 5=直径、2=线性。尺寸显示文本在 `GetText(1)`，`GetText()` 返回空串。
- `GetDimension2` 在 `IDisplayDimension` 上，`IAnnotation` 没有；删除/测量前先 `win.Dispatch(ann.GetSpecificAnnotation)`。
- 读 `GetName` / `GetAnnotation` 一类必须**先 Dispatch 再取**（属性/方法两种形态都兼容），否则静默 None。
- `AddDimension2` 不报错也可能给错类型：两平行边产出角度、成角边产出长度。必须查 `GetDimension2(0).GetType`，类型不对就 `GetAnnotation→Select(False)→EditDelete→EditRebuild3` 撤销。
- `InsertSurfaceFinishSymbol3` 忽略传入 x/y（落在图纸原点），必须 `IAnnotation.SetPosition` 并回读；PDF 像素自检映射 `px = x/sheet_w*W`、`py = (1-y/sheet_h)*H`。
- 草图几何：`GetSketchSegments/GetSketchPoints/GetSketchContours.IsClosed/seg.GetType(0线1弧)/GetLength/GetCurve.LineParams/CircleParams` 可用；`ModelToSketchTransform`（MEMBERNOTFOUND）、`GetReferenceEntity`（需 out 参数）、`ISketchLine.GetStartPoint2` 不可用 → `plane_frame` 恒 `null` + `plane_frame_available:false`，坐标为草图局部坐标。圆弧端点由点集反解（弦长 `2r·sin(L/2r)`），不唯一就 `null` + `endpoints_source` 写原因。
- `NewSheet4` 在此 build 上**不套图纸格式**（`.drwdot` / `.slddrt` / `SetTemplateName+ReloadTemplate` 三路实测无效）；`GetTemplateName()` 回显请求值、**不能自证**。故 `add_sheet` 的 `template_applied` 等字段恒 `null` + 硬警告，多页正式交付走「多 `.SLDDRW` + 外部合并」。
- `place_outside` 只对点状标注成立：角度尺寸的引线是绕顶点的弧，外推会越框 → 角度/弧长一律不参与外置（记 `outside_debug.skipped_angular`）。
- 剖切：`offset_m = (origin[dominant] - centre[dominant]) * scale`；sanity 是几何事实——垂直水平轴的剖切不改变宽度，故剖视宽必须等于源视图宽（容差 0.10）。偏心弦剖要用**绝对** `SECTION_OFF_AXIS_TOLERANCE_MM=0.1` 同时比非 kept 维。
- 孔标注：`AddHoleCallout2` 返回值必须 `Dispatch` 再 `GetText(slot)`；`text` 只放一条原生文字，多槽进 `text_lines`（**深度门必须判 `" ".join(text_lines)`**，`<HOLE-DEPTH>` 可能在第二槽）；`<MOD-DIAM>` 会渲染成 Ø 必须保留，只剥 `<hw-*>`。贯通/盲孔不可判 → `through_state:"unknown"`。
- `AddHoleCallout2` 对某些几何恒返 null（与选择方式无关）→ `verdict: no_native_callout`，不要让调用方反复调 offset。
- 公差渲源自证：SolidWorks 常把 `+`/`-` 导出成**独立文本对象**，文本层可能连符号都没有 → 只有「窗口内带符号 token」可判 `sign_text`；裸数字/整页回退一律 `evidence: numeric_magnitude` + `render_matches: null`（非通过非失败），并在 warning 里说明这是能力边界。
- 文档清理：`app.GetDocuments` / `RevisionNumber` 是属性；`app.CloseDoc(title)`；只关临时目录文档。smoke 被强杀会留同名文档导致后续 `OpenDoc6 errors=65536`，启动按 `assetmate_smoke_` 前缀自愈。

## 能力边界（不要重复投入）

- `plane_frame`（基准面 origin/axes）：本 binding 补探 6 条 API 全不可用，已结案，保持 `null`。
- 新页图框/标题栏：API 天花板，不自绘图框（成本 > 收益，已与用户确认）。
- 公差 token 归并（路线 A）：本机文本层无符号 token，归并无法验证，未做。
