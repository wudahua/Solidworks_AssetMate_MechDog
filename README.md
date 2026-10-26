# Solidworks_AssetMate_MechDog

![Before: all parts white / After: 19 part types in distinct colors](docs/images/recolor-before-after.png)

SolidWorks 小工具集，以 MCP 服务器形式提供给 AI 智能体。GitHub 仓库名与 MCP 注册名统一为 **`Solidworks_AssetMate_MechDog`**。

第一个能力来自真实项目：给装配体按零件类型批量上色，让整个装配体五颜六色、一眼可分。核心 COM 逻辑已在 SolidWorks 2024 实测验证。

**这个项目的差异化不是"会调 API"，而是把 SolidWorks COM 的坑全部固化进代码和文档。** 踩坑清单见 [docs/com-quirks.md](docs/com-quirks.md)。

## 工具（v0.1）

| 工具 | 作用 | 写入？ |
|---|---|---|
| `recolor_assembly` | 按零件类型批量上色；默认 dry-run，`apply=true` 才修改；返回原色值可回退 | 可选 |
| `get_assembly_tree` | 组件树：零件名、路径、当前颜色、子装配递归 | 否 |
| `get_mass_properties` | 整机质量/体积/表面积/质心（SI 单位；实验性，失败返回结构化错误） | 否 |
| `export_preview` | 导出等轴测等视图图片（BMP 原生，PNG 需 Pillow） | 仅图片 |

所有工具返回结构化 JSON，出错时附 `hint` 指向对应坑的编号。

## 环境

- Windows + SolidWorks 2020+（已在 2024 验证）
- 64 位 Python 3.10+，与 SolidWorks 位数一致

## 安装

```bash
pip install .
# 需要 PNG 输出时：
pip install ".[png]"
```

## 在 AI 智能体中注册

MCP 配置（以 Claude Desktop / 兼容客户端为例）：

```json
{
  "mcpServers": {
    "Solidworks_AssetMate_MechDog": {
      "command": "solidworks-assetmate-mechdog"
    }
  }
}
```

开发模式可用 `"command": "python", "args": ["-m", "solidworks_assetmate_mechdog.server"]`。

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
| `export_preview` 1600×1200 | 4 s |
| 全流程 tree → dry-run → apply → 复核 → 预览 | 7 m 35 s（未加缓存时 10 m 33 s） |

结论：慢在 SolidWorks 的 COM 往返和文件加载，不在 Python 侧。单个装配首次操作请预留 2–3 分钟，不要用默认 30–60 秒的工具超时。

## 当前局限

- 只改组件外观，不改材质/密度；不含面级、特征级外观。
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
