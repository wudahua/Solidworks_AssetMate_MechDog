# 项目长期约定

- 项目：`Solidworks_AssetMate_MechDog`，SolidWorks 小工具集 MCP；GitHub 仓库名与 MCP 注册名必须完全一致，GitHub 账号 `wudahua`。
- 远端：https://github.com/wudahua/Solidworks_AssetMate_MechDog（PUBLIC，分支 main），gh CLI 已登录。
- 结构：工作区根即仓库根；`src/solidworks_assetmate_mechdog/`；工具在 `tools/`，COM 统一走 `sw_session.py`。
- 依赖：MCP SDK 固定 `mcp>=1.2,<2`（FastMCP API）；Windows + pywin32；Python 包名小写，注册名保留大小写。
- 工具规范：全部返回 JSON；写操作默认 dry-run、执行前快照原值；慢 COM 调用只取一次；COM 不可用不得编造结果。
- 工具集决策（2026-10-26）：6 个工具全部保留，不做精简（含实验性的 `get_mass_properties`）。
- 文档规范：每个新工具同步更新 `docs/com-quirks.md` 踩坑清单；这是项目核心差异化。
- `recolor-GitHub-交接/` 是本地交接资料，不发布、已 gitignore。
- 测试约定（2026-10-31）：`python tests/check_all.py` 离线回归（27 条断言）+ `python tests/smoke.py` 真机（31 项，裸跑约 15 分钟，须后台跑避免 10 分钟被杀）；smoke 里每个 `模块.函数(...)` 调用由 `regressions.py` 断言绑定到 tools 层真实签名，改签名必须同步改 smoke；改完必须跑一次真实调用再发布。
- 出图几何口径（2026-10-31，P2-13）：`IView.GetOutline` 宽高各恒定虚胖 `OUTLINE_MARGIN_M = 0.0119`（11.9 mm，与视图/比例无关，实测 4 视图 1:1 与 2:1）。所有 overlap / 出界 / 打包判断一律用 `drawing.precise_outline`（常数内缩，O(1) 零 COM）；`ink_outline()` 逐可见边顶点重算在 25 特征零件上约 200 s，只做一次性交叉校验，**绝不**放进 audit 或 `_place_views` 循环。`ink_outline` 必须用 `paper = view.Position + (model - centre) * scale` 投影成图纸米，不能返回模型坐标。
- 出图标注口径（2026-10-31，0.8.19）：尺寸身份是 `IDimension.FullName`，不是 `IAnnotation.GetName` 的 `细节项目N`（同一驱动尺寸跨视图插入会得到不同标签）；`eliminate_duplicates` 靠 SW 自带标志无效，必须按 full name 分组删除副本且只动本次插入的标注（插入前 `before_names` 快照），绝不碰用户已有尺寸。`IDisplayDimension.GetAnnotation` 必须 `call_method` 按方法调用才能读 `GetPosition`（属性派发拿不到）；`IAnnotation.GetType == 4` 才是尺寸，note/GTOL/粗糙度是别的类型（单尺寸移动先查类型）；`EditRebuild3` 是方法不是属性，插入后必须真调用才能让 `view.GetAnnotations` 刷新。
- 局部视图 / 形位公差 / 粗糙度口径（2026-10-31，0.8.20）：`add_detail_view` 的 `scale` 是**请求比例**，不能被 `target.ScaleDecimal`（父视图比例）覆盖——父比例只用于边界圈草图坐标换算；判空用 `GetVisibleComponents` + `GetVisibleEntities2(comp, 1)` 数可见边，`edge_count == 0` 才是真空视图；比例被锁必须返回 `detail_scale_locked` 错误而不是静默 `ok:true`。`<MOD-*-PAR>` 在 `SetFrameSymbols2` 里**不会**解析成 GD&T 几何符号，只当字面文本，所以符号格改写中文文字（如「位置度」），并用 `GetFrameSymbols2(1)` 回读校验，不等就删框返回 `gtol_symbol_not_applied`（`SetFrameSymbols2` 返回 None，只能靠回读判成败）。`InsertSurfaceFinishSymbol3` 会**忽略传入 x/y**，符号实际落在图纸原点，必须 `IAnnotation.SetPosition` 修正并回读 `position_actual_m`；PDF 像素自检要把图纸左下原点映射到 PNG 左上：`px = x/sheet_w*W`、`py = (1-y/sheet_h)*H`，±50 px 窗口 `ImageChops.difference` 变化像素 ≥20 判可见；DWG 是矢量无像素通道，只做文件非空检查。
- 测试环境坑：smoke 被 SIGTERM 强杀会残留同名文档，下次全部 `OpenDoc6 errors=65536`；smoke 启动自愈按路径前缀 `assetmate_smoke_` 关闭残留（有回归断言守护）。COM 属性陷阱补充：`app.GetDocuments` / `app.RevisionNumber` 是属性不是方法，清理文档用 `app.CloseDoc(title)`，只关临时目录文档、绝不动用户文档。
- `用户反馈问题/` 是本地反馈资料，不发布、已 gitignore（2026-10-30）。
