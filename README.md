# Solidworks_AssetMate_MechDog

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

## 当前局限

- 只改组件外观，不改材质/密度；不含面级、特征级外观。
- 不做配置/显示状态切换；子装配递归仅 `get_assembly_tree` 支持。
- 读到的外观可能来自内存文档而非磁盘；核对文件真实状态需先 `CloseAllDocuments`（见坑清单第三节）。

## 路线图

- v0.2：BOM 导出、批量 STEP/PDF/DXF、组件重命名、干涉检查
- v0.3：特征树与配合导出，对接 IR 逆向工程流水线

## License

MIT © 2026 wudahua
