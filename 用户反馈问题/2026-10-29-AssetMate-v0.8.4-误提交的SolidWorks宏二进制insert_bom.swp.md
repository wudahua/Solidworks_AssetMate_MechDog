# 更新问题反馈：宏 pilot 阶段的二进制文件 insert_bom.swp 被误提交（无代码引用、不进 wheel、扩展名歧义）

> 反馈日期：2026-10-29
> 项目版本：solidworks-assetmate-mechdog **v0.8.4**（commit f8bc23c，自 v0.8.2）
> 问题性质：**仓库卫生**——一个已失效的 SolidWorks 宏二进制遗留物被跟踪入库
> 更新方式：git pull（e3aef6c → f8bc23c，fast-forward）

---

## 一、更新验证基线（全部通过）

| 检查项 | 结果 |
|--------|------|
| git pull | ✅ fast-forward，无冲突 |
| pyproject.toml | ✅ 仅版本号 0.8.2 → 0.8.4，依赖零变化，editable 自动生效 |
| MCP 握手 | ✅ `serverInfo: v0.8.4`，48 个工具 |
| tests/static_check.py | ✅ `static check OK (48 tools, every module reference resolves, README in sync)` |

**上版（v0.8.2）反馈已全部落地，且是按建议原文落实的：**
- README 路线图补齐 `v0.6 ✅ / v0.7 ✅ / v0.8 ✅`（436–438 行）
- docs/ROADMAP.md 现状更新为「v0.8.2，2026-10-29」，并加了「本节是写入时的快照」声明（与建议措辞一致）
- static_check 新增 **stale roadmap guard**（commit 52758ca），路线图纳入守护
- 顺带感谢 CONTRIBUTING.md 的「run-it-after-you-change-it」规则——正是这次 P0 wrapper 签名漂移该有的免疫机制

---

## 二、核心问题：`insert_bom.swp` 被误提交

### 2.1 它是什么

`src/solidworks_assetmate_mechdog/resources/insert_bom.swp`（25,600 字节）：
- 文件头 `D0 CF 11 E0 A1 B1 1A E1` 是 **OLE2 Compound Document** 签名——这是 **SolidWorks 录制的 VBA 宏工程文件**（SolidWorks 宏的 `.swp` 扩展名），对应 BOM 攻坚期的 recorded macro 路径（commit 0eabc6f "add_bom_table via recorded macro (pilot)"）。

### 2.2 为什么判定它是遗留物

1. **代码零引用**：全 `src/` 检索 `swp` / `macro` / `RunMacro` 均无匹配。`add_bom_table`（layout.py:403）最终走的是直接 API 路径（commit d4a795d 后宏路径已被替代）。
2. **不进发行包**（实测）：本机构建 wheel 后检查，**包内不含 `insert_bom.swp`，也不含 `resources/` 目录**——pyproject.toml 没有 `package-data` 配置，也没有 MANIFEST.in。也就是说：即使它有运行时价值，`pip install .`（README 主推方式）装出来的包也拿不到它；目前恰好因为代码不引用而无害。

### 2.3 三个危害（都不大，但值得处理）

| 危害 | 说明 |
|---|---|
| **扩展名歧义** | `.swp` 同时是 vim 交换文件的扩展名。任何协作者（或未来的清理脚本/`.gitignore` 模板）看到 `resources/insert_bom.swp` 都会先入为主认为是交换文件垃圾——而它其实是 OLE2 二进制。我本次排查就花了三轮才定性。 |
| **仓库噪音** | 25.6KB 二进制进了 git 历史，且 resources/ 目录因它被创建，给后来者"这里应该有运行时资源"的错觉（实际 wheel 里不会有）。 |
| **埋雷** | 如果未来真想恢复宏方案并把它当资源加载，会直接踩中"package-data 缺失 → wheel 缺文件 → 干净安装崩溃"的坑，而且错误会在用户侧才暴露。 |

### 2.4 修复建议

```bash
git rm --cached src/solidworks_assetmate_mechdog/resources/insert_bom.swp
rm src/solidworks_assetmate_mechdog/resources/insert_bom.swp   # 如需留档，移到 项目规划/ 目录即可
```

并在 `.gitignore` 加一条（注意注释区分，避免未来误伤真正的 SW 宏资源）：

```gitignore
# vim swap files（注意：SolidWorks VBA 宏也是 .swp，若未来入库请用否定模式排除）
*.swp
```

---

## 三、一条防雷建议（当前无害，未来防坑）

如果将来确有二进制资源需要随包分发（宏、模板等），pyproject.toml 需要补：

```toml
[tool.setuptools.package-data]
solidworks_assetmate_mechdog = ["resources/*"]
```

并让 static_check 顺带校验「src 下被引用的资源文件必须出现在 wheel 清单里」——同样一行正则的量级，把发行完整性也纳入四道防线。

---

## 四、结论

v0.8.4 代码层零问题，路线图反馈闭环完成，P0 修复 + wrapper-vs-impl 签名守护体现了很好的自愈能力。唯一需要处理的是 `insert_bom.swp` 这一个误提交文件：`git rm` 一条命令的事，顺手把 package-data 的坑也标注一下即可。

— 反馈人：WorkBuddy 智能体（同一环境，Windows 10 / Python 3.13.14 64 位 / venv editable 安装 / SW2020+）
