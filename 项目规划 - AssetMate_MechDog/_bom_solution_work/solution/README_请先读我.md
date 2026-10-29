# BOM 解决方案包（已实测通过）

已在 SOLIDWORKS 2024 SP4 上完成：**原生 TopLevelOnly BOM，10 行 × 4 列，含 9 行数据与数量列，保存重开保持。**

## 先看这些

1. `文档/解决方案与集成说明.md`：根因、最终调用顺序和集成步骤。
2. `代码/bom_native.py`：完整可运行实现。
3. `代码/layout_add_bom_table.py`：现有工具函数替换示例。
4. `成功案例/BOM_SOLVED.SLDDRW`：实际生成的成功图纸。
5. `验证结果/`：端到端、复开和实验对照的 JSON 证据。

## 核心调用顺序

```text
Indented 建表 → 有数据后切 TableType=TopLevelOnly
→ 隐藏最后一个重复 Quantity 列 → 重建保存
```

不要再走 `SetConfigurations`，也不要用 `SetColumnType` 补 Quantity 列。

## 快速验证

编辑 `脚本/01_端到端生成验证.py` 顶部路径，然后运行：

```bash
python 脚本/01_端到端生成验证.py
python 脚本/02_复开验证.py
```

集成后必须重启 MCP server，并确认没有重复的 `def add_bom_table`。
