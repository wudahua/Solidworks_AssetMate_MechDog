# BOM 明细栏：现状与目标（请先读这份）

> 打包时间：2026-10-29 13:00
> 环境：SOLIDWORKS 2024 SP4（32.4.0）/ Python 3.11 / pywin32 晚绑定
> 一句话：**表格能创建出来了，但只有表头，没有数据行和数量列。**

---

## 一、最终要的效果

在装配工程图上插入**原生 BOM 明细栏**，要求：

```
表格内容与界面手工插入一致：

rows=10  cols=4
r0: ['项目号', '零件号', '说明', '数量']     ← 4 列（含数量）
r1: ['1', 'SAB1_0100_000', '框架模块', '1']
r2: ['2', 'SAB1_0200_000', '扫描笔固定模块', '1']
...
（共 9 行数据，与装配的 9 行一一对应）
```

**判定标准**（缺一不可）：

| # | 标准 |
|---|---|
| 1 | `RowCount > 1`（有数据行） |
| 2 | `ColumnCount == 4`（含数量列） |
| 3 | `BomFeature` 可取（原生而非画线） |
| 4 | 内容与 `export_bom` 对账一致 |

---

## 二、现在到哪一步

```
rows=1  cols=3
header: ['项目号', '零件号', '说明']          ← 缺「数量」列
BomFeature: 可取到
GetReferencedModelName: 正确指向 SAB1_0000_000.SLDASM
```

| 项 | 状态 |
|---|---|
| 创建原生 BOM 表 | ✅ **成功**（脚本 + 工具双路径） |
| BomFeature | ✅ |
| 模型引用 | ✅ 正确 |
| 表头 | ✅ 正确（3 列） |
| **数量列** | ❌ 缺 |
| **数据行** | ❌ 缺 |

---

## 三、已确认的关键事实（不用重试）

### 3.1 能创建表格的正确参数

```python
table = view.InsertBomTable4(
    False,                  # UseAnchorPoint
    0.24, 0.19,             # X, Y（图纸米坐标）
    1,                      # AnchorType = TopLeft      ← 必须 1，传 0 直接返回 None
    2,                      # BomType = TopLevelOnly    ← 1 是 PartsOnly
    '',                     # Configuration（TopLevelOnly 传空串）
    template,               # <install>\lang\chinese-simplified\bom-standard.sldbomtbt
    False,                  # Hidden
    0,                      # IndentedNumberingType
    False,                  # DetailedCutList
)
# → BomTableAnnotation
```

**`AnchorType=1` + `BomType=2` 是决定性的**——此前传 `0` 和 `1`，每次直接返回 `None`，导致误判为「API 不可用」。

### 3.2 最关键的新证据：总列数 5，可见 3

```
当前表：  RowCount=1   ColumnCount=3   TotalColumnCount=5
宏成功表：RowCount=10  ColumnCount=4   TotalColumnCount=5
```

**两边 `TotalColumnCount` 都是 5。**

所以：表格**定义了 5 列**，我的表只**显示 3 列**，宏成功的表显示 4 列。

**数量列不是「不存在」，是「没渲染」。**

### 3.3 已排除的方向（都试过，都无效）

| 方向 | 尝试 | 结果 |
|---|---|---|
| 配置名 | `''` / `'Default'` / `'UP'` 九种组合 | 全 3 列 |
| `SetConfigurations` | 1 参 / 3 参、list / tuple / `VT_ARRAY\|VT_BSTR` / `VT_ARRAY\|VT_VARIANT` | 返回 True/False，**列数始终 3** |
| 缺失项策略 | `KeepMissingItems=True` / `StrikeoutMissingItems=True` | 列数不变 |
| 重建 | `EditRebuild3` / `ForceRebuild3` / `GraphicsRedraw2` | 执行成功，行数不变 |
| `BomType` | 1 / 2 / 3 | 全 3 列 |
| 隐藏列 | 读 `ColumnHidden(3)/(4)` → 都是 `False`；PROPERTYPUT 写入 | **反而 3 列 → 2 列**（改坏了） |
| 模板 | `bom-standard.sldbomtbt` | 未做多模板对照 |

---

## 四、卡在哪（一句话）

**配置没有关联到 BOM 表格，导致数量列不渲染、数据行不生成。**

`SetConfigurations` 返回 `True` 但不起作用；`ColumnHidden` 读出来是 `False` 但列数对不上。

**这两个信号互相矛盾，说明我对 `TotalColumnCount` / `ColumnCount` / `ColumnHidden` 的语义理解有误**，需要规划者判断。

---

## 五、需要规划的三种可能

`TotalColumnCount=5` 但 `ColumnCount=3`，三种解释：

| 可能 | 含义 | 下一步 |
|---|---|---|
| **A** | `TotalColumnCount` = 模板定义列，`ColumnCount` = 实际渲染（有数据的）列 | 回到配置关联；或 `InsertColumn2 + SetColumnType3(203)` 直接补列 |
| **B** | 隐藏列，但 `ColumnHidden` 的索引与列索引不一一对应 | 找正确索引基准重试 |
| **C** | `ColumnHidden` 是 1-based | 用 `(4)` / `(5)` 重试 |

**我倾向 A**：列数差异反映「没关联上配置所以不渲染」，而不是「被隐藏」。这也与「配置数量列未生成」的旧结论一致，只是换了个解释角度。

---

## 六、还没试的两条（成本低，建议先做）

### 6.1 多模板对照（5 分钟）

```python
cases = [
    ("default-empty", ""),
    ("cn-standard", ...\chinese-simplified\bom-standard.sldbomtbt),
    ("cn-all",      ...\chinese-simplified\bom-all.sldbomtbt),
    ("cn-material", ...\chinese-simplified\bom-material.sldbomtbt),
    ("en-standard", ...\english\bom-standard.sldbomtbt),
]
# 每个用新图纸，记录 RowCount / ColumnCount / TotalColumnCount
```

### 6.2 `InsertColumn2` + `SetColumnType3(203)`（10 分钟）

```python
QUANTITY = 203   # swBomTableColumnType_Quantity

table.InsertColumn2(<Last位置常量>, 0, "数量", <默认宽常量>)
table.SetColumnType3(table.ColumnCount - 1, QUANTITY, True, None)  # PropData=None 是诊断性质
call_method(doc, "ForceRebuild3", False)
```

**阻塞点**：`GetColumnType3` 在这个绑定下 `DISP_E_TYPEMISMATCH`，读不到宏成功表那列的 `PropData`，只能传 `None`。

**请判断：`PropData=None` 值不值得试？**

---

## 七、包内容清单

| 文件 | 说明 |
|---|---|
| `00_请先读我_现状与目标.md` | 本文件 |
| `成功案例_BOM_TEST.SLDDRW` | **宏插入成功的图纸**（10 行 4 列，可对照） |
| `宏/insert_bom.swp` | 界面录制的宏（能成功插入 BOM） |
| `代码/layout_add_bom_table.py` | 当前实现（含注释说明） |
| `代码/sw_session_call_method.py` | 属性派发陷阱的统一封装 |
| `脚本/01_最简可复现_能创建表格.py` | 已验证可创建（rows=1） |
| `脚本/02_诊断_列数与配置.py` | 打印 ColumnCount / TotalColumnCount / 配置状态 |
| `文档/*.md` | 14 份历史记录（按时间顺序，可跳过） |

---

## 八、版本与提交状态

```
main @ 066e394, ahead 9（GitHub 502 未推送）
48 工具, static check 全绿
capabilities.bom_table = partial
```

---

## 九、一句话交代

**创建层已经彻底解决**（`AnchorType=1`、重复函数覆盖、两处调用错误都已定位并修复）。

**剩下只有「配置关联 → 数量列 → 数据行」这一条链**，而我对 `TotalColumnCount` 与 `ColumnCount` 的关系判断不了，这是唯一需要外部判断的地方。
