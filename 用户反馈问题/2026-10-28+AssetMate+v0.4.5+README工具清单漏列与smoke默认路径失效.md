# 2026-10-28 AssetMate v0.4.5 验收反馈：README 工具清单漏列 2 个工具；smoke 默认模型路径不在仓库

> 反馈人环境：Windows 10 x64 · SolidWorks 2026 SP0（RevisionNumber 34.0.0，验收时正在运行）· Python 3.13.12 (64-bit) · mcp 1.30.0 · WorkBuddy MCP 客户端
> 验收对象：`wudahua/Solidworks_AssetMate_MechDog` @ main（5d26393，v0.4.5）
> 结论：**v0.4.5 验收通过**。v0.1.7 → v0.4.5 大版本扩展（8 → 35 个工具，新增工程图闭环与自检判定）功能与回归全部正常；上轮 2 条观察项全部落实。本轮发现 2 个文档/打包层面的轻微问题。

---

## 一、大版本扩展验收（全部通过 ✅）

### 1.1 规模与注册

- stdio 握手：`serverInfo.version == "0.4.5"` ✅
- **tools/list 实际注册 35 个工具**（v0.1.7 为 8 个）：装配零件 13 + 工程图 16 + 自检判定 6
- 新增模块：`drawing.py`（2672 行，81 处结构化错误返回）、`export.py`、`ledger.py`、`material.py`、`survey.py`、`tests/smoke.py`
- `error()` 支持 `**extra` 扩展字段（no_match 附 available_parts 即依赖此能力）

### 1.2 上轮观察项落实核验（全部按建议修复 ✅）

| 上轮观察项（#v0.1.7） | v0.4.5 处理 | 实测 |
|---|---|---|
| `no_match` 守卫对 dry-run 也生效，丢失探索性 parts 列表 | dry-run 零匹配恢复 `ok=true`，附 `available_parts` 与引导文案；仅 `apply=true` 时拒绝并附 `available_parts` | dry-run 零匹配返回 `ok=true, available_parts=['2','3','4','P2_1','P3_6','P4_5','T1_Pieza1']` ✅；apply 零匹配 `no_match` + `available_parts` ✅（两种场景都验证） |
| `readback_sample` 上限 8 建议文档标注 | 已写入 `recolor_assembly` docstring（"readback covers at most 8 part types (1 instance per palette key)"） | ✅ |

### 1.3 回归验证

- `get_mass_properties` 的 `mass_basis` 披露保持 ✅（本机 4.248158 kg / density 1000.0）
- not-found 错误信息保持带原因 ✅
- **官方 smoke 测试 11/11 通过**（1m47s，`--skip-export --skip-drawing`，用本机 10 组件装配 + P2_1 零件）：连接/版本、组件树、错误信息、BOM+CSV、质量属性、上色 dry-run、零匹配守卫、上色 apply（内存，读回验证）、实体列表、实体分色、PNG 预览（无 BMP 残留）
- 可编辑安装 0.4.5 ✅

### 1.4 文档质量亮点

com-quirks 新增第十二（STEP 导出）、十三（工程图，约 15 个子坑：模板反斜杠、本地化视图名 `*前视`、晚绑定属性型方法、剖切线坐标系标定、GB 标题栏占位文字等）——继续延续"每个坑可复现、有实测依据"的风格。README 用 6346 张真实图纸的抽样统计（尺寸中位 11、粗糙度 60% vs 形位公差 7%）修正判定阈值并给出误报率对比（79/100 → 6/100），判定标准可追溯，值得肯定。

---

## 二、本轮发现（轻微，文档/打包层面）

### 2.1 README 工具清单漏列 2 个工具，数量对不上

- README 声称三个能力域 **13 + 15 + 6 = 34** 个工具；
- `tools/list` 实际注册 **35** 个；
- 逐项核对：装配表 13 项与实际一致；自检表 6 项一致；**工程图表只列了 14 项**（表头却写 15），实际工程图域有 **16** 个；
- **漏列**：`add_hole_callouts`（批量孔标注，单数版 `add_hole_callout` 已列）、`list_circular_edges`（列圆边，`add_hole_callouts` 的前置工具）。

建议：两行表格补齐，表头改为 16（或合并描述），总数 34 → 35。漏列的工具恰恰是孔标注工作流（list_circular_edges → add_hole_callouts）的入口，容易被使用者跳过。

### 2.2 `tests/smoke.py` 默认模型路径指向仓库外，README 的裸命令会直接失败

`smoke.py` 的默认路径：

```python
DEFAULT_ASSEMBLY = os.path.join(ROOT, "测试用例", "监视器显示器固定支架-rebuild", "装配体_显示器固定支架.SLDASM")
DEFAULT_PART = os.path.join(ROOT, "测试用例", "履带-rebuild", "P05_karetku.SLDPRT")
```

`测试用例/` 目录**不在仓库中**（zipball 无此目录，模型可能因体积/版权未入库）。README「真机回归」一节写的 `python tests/smoke.py` 在 clone/下载的仓库里会因文件不存在直接退出（exit 2）。本次验收是用 `--assembly/--part` 显式指定本机模型跑通的。

建议（任选）：
- README 该节补一句：「仓库不含测试模型，需 `--assembly/--part` 指定自己的装配和零件」；
- 或在仓库放一个极小的示例装配（如当前 sandbox 级别的几个零件）让默认路径可跑。

### 2.3 附带核实（非问题）

- README 说 smoke 共 13 项：11 项常规 + `--skip-export/--skip-drawing` 跳过的 STEP 导出与工程图链路 2 项，数目一致 ✅；
- `server.py` 的 `recolor_assembly` docstring 同步更新了零匹配语义 ✅。

---

## 三、验收记录（本机，v0.4.5 @ 5d26393）

| 验证项 | 结果 |
|---|---|
| `serverInfo.version` / tools/list | `0.4.5` / 35 工具 ✅（README 写 34，见 2.1） |
| dry-run 零匹配 | `ok=true` + `available_parts` ✅（上轮观察项①） |
| apply 零匹配守卫 | `no_match` + `available_parts`，不保存 ✅ |
| `mass_basis` 披露 / not-found 信息 | 保持 ✅ 回归通过 |
| smoke 测试（本机模型，跳过慢项） | 11/11 PASS，1m47s ✅ |
| 安装 | editable 0.4.5 ✅（注：本机 Python 环境的 setuptools/wheel 在两次会话间被动过丢失，重装后正常——与项目无关，仅记录） |

*以上基于 main 分支 5d26393 实测。从 v0.1.3 至 v0.4.5 共五轮验收，每轮反馈均获修复，本round无代码缺陷，仅文档同步问题。*
