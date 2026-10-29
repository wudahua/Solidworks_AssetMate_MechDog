# 2026-10-27 AssetMate v0.1.4 验收反馈：bad_input 错误信息丢失"文件不存在"原因

> 反馈人环境：Windows 10 x64 · SolidWorks 2026 SP0（RevisionNumber 34.0.0，验收时正在运行）· Python 3.13.12 (64-bit) · mcp 1.30.0 · WorkBuddy MCP 客户端
> 验收对象：`wudahua/Solidworks_AssetMate_MechDog` @ main（a6204b0，v0.1.4）
> 结论：**v0.1.4 验收通过**。上一轮反馈（#v0.1.3）的三类问题全部修复到位；本轮发现一个轻微新问题，建议下版优化。

---

## 一、上轮反馈修复核验（全部通过 ✅）

| 上轮问题 | v0.1.4 修复方式 | 本机实测结果 |
|---|---|---|
| **核心**：握手 `serverInfo.version` 返回 mcp 库版本（1.30.0）而非包版本 | `server.py`：`mcp._mcp_server.version = __version__`（FastMCP 1.x 无 version 参数，改在低层 Server 属性上赋值，并 pin `mcp<2` 防大版本破坏） | stdio 握手返回 `serverInfo.version == "0.1.4"` ✅；文档给出的官方验证方式 `create_initialization_options().server_version` 也返回 `0.1.4` ✅ |
| README 裸命令依赖 Scripts 在 PATH | `python -m` 写法升为推荐，多 Python 环境建议绝对路径 | 采用 `python -m` 注册，启动正常 ✅ |
| 弱网/镜像安装失败排查困难 | README 新增「常见安装问题」表 + 新文档 `docs/mcp-server-notes.md`（含 403 换阿里云源方案） | 文档与实测结论一致 ✅ |

**回归验证**（v0.1.4 全量复跑）：

- stdio JSON-RPC：initialize → initialized → tools/list，protocol 2024-11-05，工具 8/8 注册（recolor_assembly、list_bodies、recolor_bodies、export_bom、rename_components、get_assembly_tree、get_mass_properties、export_preview）
- COM attach（`GetActiveObject`，SolidWorks 运行中）：OK，RevisionNumber 34.0.0
- 错误路径实测（tools/call 传不存在的文件路径）：服务不崩溃，返回结构化 JSON（详见下节）
- `pip install -e ".[png]"` 重装元数据正常识别为 0.1.4

---

## 二、本轮新发现：`bad_input` 错误只有裸路径，丢失了"文件不存在"这个原因

### 现象

对只读工具 `get_assembly_tree` 传入不存在的路径，返回：

```json
{
  "ok": false,
  "error": "bad_input",
  "message": "E:\\__definitely_not_exists__\\nope.SLDASM"
}
```

`message` 只是 `str(FileNotFoundError(path))` 的输出——**一个裸路径**。注意 Python 的 `FileNotFoundError` 只有在 OS 抛出（带 errno）时才会生成 `[Errno 2] No such file or directory: '...'` 这样的完整文案；而 `sw_session.open_document` 是**主动 `raise FileNotFoundError(path)`**，不带 errno，所以字符串化后只剩路径。客户端（尤其是 AI 智能体）拿到后无法直接判断是「路径写错」「文件被移动」还是「扩展名不对」，只能靠猜。

### 影响范围

6 个工具共用同一模式（`except (FileNotFoundError, ValueError) as exc: return error("bad_input", str(exc))`）：
`assembly.py`（get_assembly_tree / get_mass_properties / export_preview）、`bodies.py`（list_bodies / recolor_bodies）、`recolor.py`、`bom.py`、`rename.py`——均受同样影响。

### 建议修复

在 `sw_session.open_document` 抛错时带上原因，一行改动、全工具受益：

```python
if not os.path.exists(path):
    raise FileNotFoundError(f"file not found: {path}")
```

进阶（可选）：区分两种情况给出不同提示——
- 路径不存在：建议智能体先核对盘符/目录/扩展名（.SLDASM/.SLDPRT 大小写不敏感但不可缺）；
- 路径存在但 `OpenDoc6` 返回 None：已有不错的提示（只读占用/同名文件已打开），保持即可。

### 严重性

轻微——错误本身被正确捕获、结构化返回、服务不崩溃，健壮性设计目标已达成；本条只影响错误信息的**可行动性**（actionability）。

---

## 三、观察与建议（不计为问题）

1. **`_mcp_server.version` 是私有属性**：作者已在文档中自行标注"升级 mcp 大版本时要复查"，且 pin 了 `mcp<2`，处理得当。未来 mcp 2.x 若提供官方 version 参数，建议届时切换。
2. **文档质量**：`docs/mcp-server-notes.md` 对 403/镜像/构建隔离的描述与本机实测完全吻合，"这两条属于用户环境问题，不是代码缺陷，但 README 必须写明"的定位准确。
3. **性能提示依然有效**：46 组件装配首次 `GetComponents` 约 130 s 的实测数据写进了会话层注释与 README，建议继续保持"单装配首次操作预留 2–3 分钟"的用户指引。

---

## 四、验收记录（本机，v0.1.4 @ a6204b0）

| 验证项 | 结果 |
|---|---|
| `serverInfo.name` / `serverInfo.version` | `Solidworks_AssetMate_MechDog` / `0.1.4` ✅（核心修复） |
| `create_initialization_options().server_version` | `0.1.4` ✅（作者文档方式复核） |
| tools/list | 8/8 ✅ |
| COM attach | OK（SW 2026 SP0 运行中，Rev 34.0.0）✅ |
| 错误路径（不存在的文件） | 结构化 `bad_input`，不崩溃 ✅（信息质量见第二节） |
| editable 安装元数据 | 0.1.4 ✅ |

*以上基于 main 分支 a6204b0 实测。上一轮 403 的 pip -vvv 日志仍在 `_pip_verbose.log`，如需可联系反馈人。*
