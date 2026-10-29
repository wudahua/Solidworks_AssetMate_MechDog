# 项目长期约定

- 项目：`Solidworks_AssetMate_MechDog`，SolidWorks 小工具集 MCP；GitHub 仓库名与 MCP 注册名必须完全一致，GitHub 账号 `wudahua`。
- 远端：https://github.com/wudahua/Solidworks_AssetMate_MechDog（PUBLIC，分支 main），gh CLI 已登录。
- 结构：工作区根即仓库根；`src/solidworks_assetmate_mechdog/`；工具在 `tools/`，COM 统一走 `sw_session.py`。
- 依赖：MCP SDK 固定 `mcp>=1.2,<2`（FastMCP API）；Windows + pywin32；Python 包名小写，注册名保留大小写。
- 工具规范：全部返回 JSON；写操作默认 dry-run、执行前快照原值；慢 COM 调用只取一次；COM 不可用不得编造结果。
- 工具集决策（2026-10-26）：6 个工具全部保留，不做精简（含实验性的 `get_mass_properties`）。
- 文档规范：每个新工具同步更新 `docs/com-quirks.md` 踩坑清单；这是项目核心差异化。
- `recolor-GitHub-交接/` 是本地交接资料，不发布、已 gitignore。
