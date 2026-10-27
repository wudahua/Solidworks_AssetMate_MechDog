# Solidworks_AssetMate_MechDog

![Before: all parts white / After: 19 part types in distinct colors](docs/images/recolor-before-after.png)

SolidWorks 小工具集，以 MCP 服务器形式提供给 AI 智能体。GitHub 仓库名与 MCP 注册名统一为 **`Solidworks_AssetMate_MechDog`**。

第一个能力来自真实项目：给装配体按零件类型批量上色，让整个装配体五颜六色、一眼可分。核心 COM 逻辑已在 SolidWorks 2024 实测验证。

**这个项目的差异化不是"会调 API"，而是把 SolidWorks COM 的坑全部固化进代码和文档。** 踩坑清单见 [docs/com-quirks.md](docs/com-quirks.md)。

## 工具（v0.3）

| 工具 | 作用 | 写入？ |
|---|---|---|
| `recolor_assembly` | 按零件类型批量上色；默认 dry-run，`apply=true` 才修改；返回原色值可回退 | 可选 |
| `list_bodies` | 列出零件的实体（名称、面/边/顶点数） | 否 |
| `recolor_bodies` | 多实体零件按实体分色；逐面上色保证生效；默认 dry-run | 可选 |
| `export_bom` | 导出 BOM：零件聚合、数量、材料、单件/总质量，可写 CSV | 仅 CSV |
| `rename_components` | 重命名零件：`file` 只改磁盘，`auto` 另存新文件并重定向装配引用 | 是 |
| `get_assembly_tree` | 组件树：零件名、路径、当前颜色、子装配递归 | 否 |
| `get_mass_properties` | 整机质量/体积/表面积/质心（SI 单位），含 `mass_basis` 密度披露 | 否 |
| `export_preview` | 导出等轴测等视图图片（BMP 原生，PNG 需 Pillow） | 仅图片 |
| `batch_export` | 批量导出 STEP（零件/装配）；默认 dry-run，不覆盖已有文件 | 仅新文件 |
| `create_drawing` | 从模型创建 A3 工程图 + 三视图，自动选标准比例；视图名兼容中英文 | 新文件 |
| `export_drawing` | 工程图导出 PDF / DWG / DXF | 新文件 |
| `render_drawing` | PDF 逐页渲染 PNG，供目视检查 | 新文件 |
| `drawing_audit` | 审计：视图/比例/空白视图/越界/尺寸数量 | 否 |

所有工具返回结构化 JSON，出错时附 `hint` 指向对应坑的编号。

## 环境

- Windows + SolidWorks 2020+（2024 SP4 为开发验证环境；2026 SP0 已由用户实测可用）
- 64 位 Python 3.10+，与 SolidWorks 位数一致

## 安装

```bash
pip install .
# 需要 PNG 输出时：
pip install ".[png]"
```

可选：运行标识见握手返回的 `serverInfo.version`（应与 `pyproject.toml` 一致），可在多机部署时确认装的是哪一版。

常见安装问题：

| 现象 | 处理 |
|---|---|
| 隔离构建时下载 `setuptools` 失败 | `pip install -e ".[png]" --no-build-isolation`（主环境需已装 setuptools） |
| 镜像报 `from versions: none`（实际是 HTTP 403） | 换源：`pip install -i https://mirrors.aliyun.com/pypi/simple/ ".[png]"` |
| 命令行找不到 `solidworks-assetmate-mechdog` | 该入口在 `<python>\Scripts`，通常不在 PATH；改用下方的 `python -m` 方式 |
| 工程图视图创建返回空 | 0.3.0 已处理：中文版命名视图是 `*前视`，工具会从模型自动解析；模板路径必须是反斜杠 |
| 所有工具报 `int() argument must be ... not 'VARIANT'` | 0.1.5 已修复（强制晚绑定）；升级即可，无需清理 `%TEMP%\gen_py` |

更多 MCP 侧问题（`serverInfo.version`、构建隔离、镜像 403）见 [docs/mcp-server-notes.md](docs/mcp-server-notes.md)。

## 真机回归

改完代码先跑一遍完整链路（SolidWorks 需运行）：

```bash
python tests/smoke.py
```

覆盖 12 项：连接/版本、组件树、BOM、质量属性、上色 dry-run、零匹配守卫、上色（内存不保存）、实体列表、实体分色 dry-run、预览 PNG、STEP 导出、错误信息。全部只读或写临时目录，不动仓库里的测试模型，跑完会关闭自己打开的文档。

首次运行约 5–8 分钟（解析轻化组件占大头），之后快很多。`--keep` 保留临时产物，`--skip-export` 跳过 STEP，`--assembly` / `--part` 换测试模型。

## 在 AI 智能体中注册

**推荐写法**（不依赖 PATH，最稳）：

```json
{
  "mcpServers": {
    "Solidworks_AssetMate_MechDog": {
      "command": "python",
      "args": ["-m", "solidworks_assetmate_mechdog.server"]
    }
  }
}
```

多 Python 环境时把 `command` 换成绝对路径，例如 `C:\\Python313\\python.exe`。

若 `<python>\Scripts` 已加入 PATH，也可用裸命令：

```json
{
  "mcpServers": {
    "Solidworks_AssetMate_MechDog": {
      "command": "solidworks-assetmate-mechdog"
    }
  }
}
```

## 用法示例

对智能体说："把 `D:\models\beast.SLDASM` 按零件类型上色，配色参考 examples/palette.example.json。"

等价工具调用：

```json
{
  "assembly_path": "D:/models/beast.SLDASM",
  "palette": { "Crank": [0.88, 0.20, 0.18], "Frame": [0.36, 0.40, 0.45] },
  "apply": false
}
```

确认 dry-run 返回的零件与匹配数无误后，再以 `apply=true` 执行。

多实体零件按实体分色（先查实体名，再用实体名作 palette 键）：

```json
{ "part_path": "D:/models/hull.SLDPRT",
  "palette": { "Hull": [0.33, 0.49, 0.95], "Deck": [0.95, 0.8, 0.33] },
  "apply": false }
```

实体分色走面级外观（SolidWorks 的实体级 API 实测不可靠），会覆盖原有面外观，请在副本上操作。

![Body-level recolor: 8 solid bodies, 8 colors](docs/images/recolor-bodies.png)

导出 BOM（只读，可同时写 CSV）：

```json
{ "assembly_path": "D:/models/beam.SLDASM", "csv_path": "D:/out/bom.csv" }
```

返回按零件聚合的行：`part`、`qty`、`material`、`mass_each_kg`、`mass_total_kg`、`path`，以及 `total_mass_kg` 和 `warnings`（缺失值逐条列出，不猜）。

重命名零件文件（默认 dry-run）：

```json
{ "part_paths": ["D:/parts/P01.SLDPRT", "D:/parts/P02.SLDPRT"], "prefix": "RN_" }
```

支持 `mapping` / `prefix` / `suffix`；目标已存在、非法字符、未变化都会进 `conflicts` 而不执行。`apply=true` 后失败会自动回滚。若传 `assembly_path`，会额外报告哪些组件仍引用旧文件名。

自动模式（改名 + 更新装配引用 + 保存，一次完成）：

```json
{ "part_paths": ["D:/parts/P01.SLDPRT"], "prefix": "RN_",
  "assembly_path": "D:/parts/beam.SLDASM", "mode": "auto" }
```

装配打开时会锁住组件文件，无法直接改名，因此 `auto` 采用「静默另存新文件 → 重定向引用 → 保存装配」：原文件保留、可回退；实例名会跟随新文件名（`P01-1` → `RN_P01-1`）。

## 设计原则

1. **统一会话层**：所有工具共用 `sw_session`，慢调用（如 `GetComponents`）只取一次、内存分组。
2. **写操作先快照**：默认 dry-run；执行时返回修改前的颜色，支持回退。
3. **不编造结果**：COM 不可用时返回结构化错误和坑文档指引，绝不假装成功。
4. **每个工具配一篇坑笔记**：新增工具必须同步更新 [docs/com-quirks.md](docs/com-quirks.md)。

## 性能

首次读取装配要付 SolidWorks 加载成本：46 组件实测 `GetComponents` 单次约 130 秒。组件列表按文档缓存，`get_assembly_tree → recolor_assembly → get_assembly_tree` 只在第一次付这个成本；外部增删组件后给 `get_assembly_tree` 传 `refresh=true`。

实测数据（2026-10-26，监视器固定支架，46 组件 / 19 种零件，SolidWorks 2024 SP4 / Py3.13）：

| 操作 | 耗时 |
|---|---|
| `get_assembly_tree` 首次（含零件加载） | 163 s |
| `recolor_assembly` dry-run（缓存命中） | 64 s |
| `recolor_assembly` apply（46 件上色 + 保存） | 121 s |
| `recolor_bodies` apply（899 面 / 8 实体，含保存） | 76 s |
| `export_preview` 1600×1200 | 4 s |
| 全流程 tree → dry-run → apply → 复核 → 预览 | 7 m 35 s（未加缓存时 10 m 33 s） |

结论：慢在 SolidWorks 的 COM 往返和文件加载，不在 Python 侧。单个装配首次操作请预留 2–3 分钟，不要用默认 30–60 秒的工具超时。

## 当前局限

- 只改组件外观，不改材质/密度；不含面级、特征级外观。
- 实体分色为整块上色，不提供单个面单独配色的工具；面级外观会覆盖原贴图。
- 组件**实例名**无法通过 API 修改（`IComponent2.Name` 只读、`Name2` 赋值静默无效），`rename_components` 只做文件重命名；实例名请在 FeatureManager 树中手动改。
- 某些环境（存在 `gen_py` 缓存）下组件颜色**读回**可能失真为白色，而渲染与保存正常；`recolor_assembly` 用 `readback_verified` 标记，验收请以 `export_preview` 为准，不要依赖 `previous_colors` 回退。读回抽样为每个 palette 键 1 个实例、最多 8 种零件，超出部分不做读回校验。
- 不做配置/显示状态切换；子装配递归仅 `get_assembly_tree` 支持。
- 读到的外观可能来自内存文档而非磁盘；核对文件真实状态需先 `CloseAllDocuments`（见坑清单第三节）。

## 路线图

完整规划见 [docs/ROADMAP.md](docs/ROADMAP.md)（目标、版本节奏、质量门、风险）。摘要：

- v0.2：BOM 导出、批量 STEP/PDF/DXF、组件重命名、干涉检查
- v0.3：特征树与配合导出，对接 IR 逆向工程流水线
- v0.4：操作快照与回退、日志、批量任务队列
- v1.0：接口冻结、多配置与显示状态

## License

MIT © 2026 wudahua
