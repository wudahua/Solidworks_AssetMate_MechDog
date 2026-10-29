# 2026-10-27 AssetMate v0.1.4 gen_py 早绑定缓存下 byref VARIANT 传参导致全工具崩溃

> 测试性质：MCP 全量测试（7 个工具全部覆盖）
> 测试环境：Windows + SolidWorks 2026（Revision x0x34x0 Type Library）+ Python 3.13.14 + pywin32 + AssetMate v0.1.4
> 测试模型：7 零件 + 1 总装（多连杆双曲柄滑块机构，独立沙箱目录，未触碰原始项目文件）

---

## 一、核心问题（P0，阻塞级）

**在 `gen_py` 早绑定缓存存在时（`%TEMP%\gen_py\3.13\83A33D31-27C5-11CE-BFD4-00400513BB57x0x34x0.py`，即 SldWorks 2026 Type Library），AssetMate 全部 7 个 MCP 工具 100% 崩溃**，统一报错：

```
int() argument must be a string, a bytes-like object or a real number, not 'VARIANT'
```

受影响工具（MCP 实测，全部复现）：

| 工具 | 错误码 | 结果 |
|---|---|---|
| `get_assembly_tree` | tree_failed | ❌（depth=1 与 depth=3 均失败） |
| `export_bom` | bom_failed | ❌ |
| `get_mass_properties` | （未捕获，工具层直接抛出） | ❌ |
| `list_bodies` | bodies_failed | ❌ |
| `recolor_bodies` | recolor_bodies_failed | ❌（连 apply=false 干跑也失败） |
| `recolor_assembly` | recolor_failed | ❌（同上） |
| `export_preview` | preview_failed | ❌ |
| `rename_components` | 部分成功 | ⚠️ dry-run 路径规划成功，但 `reference_check_error` 同为 VARIANT 错 |

另外 `get_mass_properties` 的异常没有被包成结构化错误信封（其他工具都有 `{"ok": false, "error": ...}` 信封），MCP 客户端直接收到裸异常。

## 二、根因定位（已复现到具体行）

通过独立 Python 脚本复现，完整 traceback 显示崩溃点在 **pywin32 早绑定 wrapper 内部**：

```
File "...\Temp\gen_py\3.13\83A33D31-...x0x34x0.py", line 60526, in OpenDoc6
    return self._ApplyTypes_(167, 1, (9, 0), ((8, 1), (3, 1), (3, 1), (8, 1),
        (16387, 3), (16387, 3)), 'OpenDoc6', ...)
File "...\win32com\client\__init__.py", line 581, in _ApplyTypes_
    self._oleobj_.InvokeTypes(dispid, 0, wFlags, retType, argTypes, *args)
TypeError: int() argument must be a string, a bytes-like object or a real number, not 'VARIANT'
```

机理：

1. `sw_session.get_sw()` 用 `win32com.client.GetActiveObject("SldWorks.Application")` 拿到的是**早绑定对象**（一旦 gen_py 缓存生成过，GetActiveObject/Dispatch 都返回 makepy 包装类）。
2. 早绑定 wrapper 对 `OpenDoc6` 的 `Errors`/`Warnings` 参数按类型库声明的 `VT_I4|ByRef (16387, 3)` 处理，pywin32 在 `InvokeTypes` 里对传入实参做 `int()` 强转——而 AssetMate 传入的是 `VARIANT(VT_BYREF|VT_I4, 0)` 对象，`int(VARIANT)` 抛 TypeError。
3. 该调用在 `open_document()` 中，是所有工具的必经入口，因此**全军覆没**；`rename_components` 的 file 模式纯文件系统路径（不打开文档）得以幸存。

关键对照实验：

- **早绑定 + byref VARIANT**：`OpenDoc6` 直接抛 TypeError（上述 traceback）。
- **强制晚绑定（`win32com.client.dynamic.Dispatch(early_obj)`）+ 同样的 byref VARIANT**：`OpenDoc6` 成功，`errors.value = 0`，后续全部 10 项测试通过。

即：**代码逻辑本身没有问题，是早绑定 wrapper 与 byref VARIANT 的传参方式不兼容**。这解释了为什么同一份代码在部分机器/部分 SW 版本上正常（无 gen_py 缓存 → 动态绑定），在有缓存的机器上全挂。

## 三、修复建议（按优先级）

1. **推荐：在 `get_sw()` 里强制晚绑定**，一行修复、对现有代码零侵入：
   ```python
   import win32com.client.dynamic as dyn
   _SW = dyn.Dispatch(win.GetActiveObject("SldWorks.Application"))
   ```
   晚绑定路径下 `VARIANT(VT_BYREF|VT_I4)` 正常工作（已实测验证），且不依赖 gen_py 缓存状态。
2. 备选：启动时检测 gen_py 缓存目录并提示/清理；或 `except TypeError` 时自动降级到 dynamic 重新包装重试一次。
3. 顺带修复：`get_mass_properties` 的外层异常也应走 `error()` 信封（当前内层 CreateMassProperty 有信封，外层 open_document 失败时裸抛）。

## 四、修复后全量回归结果（晚绑定补丁下，v0.1.4 代码未改动）

| 测试项 | 结果 | 关键数据 |
|---|---|---|
| `get_assembly_tree` depth=2 | ✅ | 10 个组件，名称/路径/RGB 齐全 |
| `export_bom` + CSV | ✅ | 7 种零件 qty 合计 10；CSV UTF-8 正常；材质未设 → 7 条 `material_not_set` warning，未瞎猜 ✅ |
| `get_mass_properties` | ✅ | mass=4.248158 kg，与 BOM 各零件质量和（4.248158）六位小数吻合 ✅；质心 [-0.3633, -0.0416, 0.4002] m |
| `list_bodies` | ✅ | Pieza1 含 1 实体 `Saliente-Extruir7` |
| `recolor_bodies` 干跑→实跑 | ✅ | 单实体精确选中后着色，未发生文档级误涂 |
| `recolor_assembly` 干跑→实跑 | ✅ | 读回 RGB [0.8471, 0.2, 0.2]（请求 0.85，sRGB 量化偏差，可忽略）；预览图红色像素连续成杆状，视觉验证通过 |
| `rename_components` file 模式 dry-run | ✅ | 路径规划正确、冲突检测正常 |
| `rename_components` auto 模式 apply | ✅ | SaveAsSilent 副本 + `ReplaceReference` 重定向成功，装配刷新后指向 `T1_Pieza1-1`；`originals_kept=True`（源码注明的设计行为，响应含 note，不算缺陷） |
| `export_preview` PNG | ✅ | 1280x960 生成；BOM 质量交叉验证见上 |
| 缓存一致性 | ✅ | recolor 后不加 `refresh=true` 也能读到新颜色，`GetComponents` 缓存未造成脏读 |

## 五、次要发现（建议 v0.1.5 顺手处理）

1. **`export_preview` 转 PNG 后残留中间 BMP**：输出 `preview_iso.png` 的同时 `preview_iso.bmp`（3.5 MB）留在磁盘未清理，建议转换成功后删除或提供选项。
2. **BOM 在材质未设时仍报质量值**：7 个零件全部 `material_not_set`，但质量列照常给出数字（SW 默认密度 1000 kg/m³ 的结果）。warning 机制是对的，但质量数字可能让用户误以为是真实材料密度，建议在 warning 或文档中明确"质量基于 SW 默认密度"。
3. **`get_mass_properties` 文档字符串标注 Experimental**，实测 `CreateMassProperty` 在本机早绑定清理后工作正常，可考虑转正。

## 六、复现与证据

- 复现脚本：`sandbox/repro_variant.py`（裸 COM 复现 + 包内复现）、`sandbox/verify_fix_latebind.py`（晚绑定补丁下 10 项全通过）、`sandbox/post_verify.py`（颜色读回/缓存/文件验证）
- 证据文件：`sandbox/bom_test.csv`、`sandbox/preview_iso.png`（红色零件可见）
- 复现条件：任一曾触发 makepy 的进程生成过 `%TEMP%\gen_py\3.13\83A33D31-*` 缓存后，后续所有 MCP 调用必现；删除该缓存可临时缓解但不可靠（其他程序会再生成）。

---

*报告基于 2026-10-27 实机全量测试，所有结论均有落盘证据，无推测项。*
