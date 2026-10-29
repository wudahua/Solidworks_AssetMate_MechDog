# 更新问题反馈：v0.6.0 README 未同步新能力（形位公差工具缺席 + 能力边界表自相矛盾 + 计数三处矛盾）

> 反馈日期：2026-10-28
> 项目版本：solidworks-assetmate-mechdog **v0.6.0**（commit 8242494，自 v0.4.9 一跳两版）
> 问题性质：**文档与代码不同步**——v0.6.0 主打新功能（形位公差可创建）在 README 多处仍是旧结论
> 更新方式：git pull（352e272 → 8242494，fast-forward）

---

## 一、先说好消息：上版反馈已被采纳，感谢

v0.4.9 时反馈的「`--no-build-isolation` 缺 setuptools 前置说明」，本次核查确认已在 v0.6.0 README 排障表中修复：

```
| 隔离构建下载 `setuptools` 失败 | Python ≥3.12 的全新 venv 先 `pip install setuptools wheel`，再 `pip install -e ".[png]" --no-build-isolation` |
```

与建议一致，且「接口报错 ≠ API 不存在」专节写得非常好——`DISPATCH_PROPERTYGET` vs `DISPATCH_METHOD` 一个标志位之差这一发现，对整个 pywin32 社区都有参考价值。

**更新验证全部通过**：依赖零变化（editable 自动生效）、MCP 握手 0.6.0、`tools/list` 返回 38 个工具、`tests/static_check.py` 通过（38 tools, every module reference resolves）。

---

## 二、核心问题：README 未同步 v0.6.0，同一份文档前后矛盾

### 2.1 主打新工具 `add_geometric_tolerance` 缺席工具表

v0.6.0 的 release commit 就是 "geometric tolerance creatable"，`tools/list` 实测返回 38 个工具且包含 `add_geometric_tolerance`，但 README「二、工程图」工具表（35–54 行）**没有它的条目**。用户读工具表会以为形位公差仍然做不了，从而绕道用 `add_dimension_tolerance` 凑合或放弃标注。

### 2.2 工具计数三处矛盾

| 位置 | 数字 | 实测 |
|---|---|---|
| 「二、工程图（18）」标题 | 18 | 表内实际 16 行；`tools/list` 中工程图类实际 17 个（16 行 + 缺席的 `add_geometric_tolerance`） |
| 三域标题相加 | 13 + 18 + 8 = **39** | `tools/list` 实际 **38**（装配 13 + 工程图 17 + 规划自检 8） |
| static_check 章节（294 行）「35 个工具函数」 | 35（0.4.9 旧数） | 实际 38，且 `tests/static_check.py` 自己输出就是 "38 tools" |

同一份 README 里 18 / 16 / 17 / 39 / 38 / 35 六个数字并存，其中只有 38 是对的。

### 2.3 能力边界表与专节自相矛盾（最影响使用判断）

「能力边界（诚实披露）」表 152 行仍写：

> **形位公差写不进去** | `InsertGtol` 报 `DISP_E_MEMBERNOTFOUND`；…两条官方路径都不可用，C# 插件也绕不过 | **只能读不能写**

但 346–368 行的专节明确承认这判断是错的（`DISPATCH_METHOD` 即可创建），且 v0.6.0 已交付 `add_geometric_tolerance`。能力边界表是调用方决策的第一入口，这条过期结论会让调用方**直接放弃一个已经可用的能力**。建议该行改写为指向专节 + `add_geometric_tolerance`，或标注「v0.6.0 已解决，见『接口报错 ≠ API 不存在』」。

### 2.4 路线图过期

379 行仍为「**v0.5 待做**：孔标注定位（性能）、形位公差（受 API 限制）、出图前规划工作流」，而 v0.5.0（hole callouts / capabilities / planner）与 v0.6.0（geometric tolerance）均已发布且三项全部落地。建议按 377–378 行的删除线风格补：

```markdown
- ~~v0.5 孔标注 / 能力声明 / 出图规划~~ ✅（0.5.0）
- ~~v0.6 形位公差~~ ✅（0.6.0）
- v0.7 待做：…
```

---

## 三、修复建议汇总（均为文档改动，代码零修改）

1. 工程图工具表补 `add_geometric_tolerance` 一行，标题改为（17）
2. 294 行「35 个工具函数」→「38 个工具函数」（或改为动态表述，避免每次加工具都要改这里）
3. 能力边界表 152 行形位公差条目改写为已解决 + 指向专节
4. 路线图勾掉 v0.5 / v0.6

**一个防再犯的小建议**：`tests/static_check.py` 已经能输出真实工具数，可以让它顺带校验 README 三个域标题的括号数字之和与实际工具数一致，不一致时警告——把「文档同步」也变成静态检查项，和这个项目「判定要有依据」的气质很配。

---

## 四、本次验证通过基线

- ✅ git pull fast-forward，无冲突；pyproject 仅版本号 0.4.9 → 0.6.0，依赖零变化，editable 安装自动生效
- ✅ MCP 握手 `serverInfo: v0.6.0`
- ✅ `tools/list` 38 个工具（新增 `add_geometric_tolerance`、`check_capabilities`、`plan_drawing`）
- ✅ `tests/static_check.py` 通过
- ✅ 上版 setuptools 排障缺口已确认修复

（更新过程中本机出现过 git 经代理 SSL 握手失败的网络问题，经排查为本地代理环境所致，与本项目无关，不计入。）

## 五、结论

v0.6.0 代码层质量很高——形位公差的 DISPATCH_METHOD 修复、能力声明工具、出图规划器都是实打实的进步；唯一需要处理的是 **README 有四处未跟上代码**，其中能力边界表的过期结论会直接误导调用方。以上均为纯文档修复，工作量约十分钟。

— 反馈人：WorkBuddy 智能体（同一环境，Windows 10 / Python 3.13.14 64 位 / venv editable 安装 / SW2020+）
