# 安装问题反馈：`--no-build-isolation` 在全新 venv 上必然失败（Python 3.12+ 未预装 setuptools）

> 反馈日期：2026-10-28
> 项目版本：solidworks-assetmate-mechdog **v0.4.6**（commit 1d7067b）
> 问题性质：**安装文档排障指引存在前置条件缺失**，按 README 排障表操作会连锁失败
> 影响人群：Python 3.12+ 用户 + 需要走镜像/内网环境的用户（恰好是 README 排障表针对的人群）

---

## 一、环境信息

| 项目 | 值 |
|------|-----|
| 操作系统 | Windows 10（10.0.19045，64 位） |
| Python | 3.13.14（64 位，满足 README 要求的 3.10+） |
| pip（venv 内） | 26.1.2 |
| SolidWorks | 已安装（C:/Program Files/SOLIDWORKS Corp，2020+ 满足） |
| 安装方式 | 全新 venv + `pip install -e ".[png]" --no-build-isolation -i https://mirrors.aliyun.com/pypi/simple/` |

---

## 二、核心问题：两条排障建议组合后连锁失败

### 2.1 复现步骤

1. `python -m venv .venv` 创建全新虚拟环境（Python 3.13）
2. 按 README「环境与安装」排障表的指引，使用隔离构建下载失败时的替代方案：

```bash
pip install -e ".[png]" --no-build-isolation
```

### 2.2 实际报错

```
pip._vendor.pyproject_hooks._impl.BackendUnavailable: Cannot import 'setuptools.build_meta'
```

完整调用栈止于 `req_install.py` 的 `editable_sanity_check` → `supports_pyproject_editable` → `_call_hook("_supported_features")`，即 pip 尝试调用 PEP 517 后端时发现 `setuptools.build_meta` 根本不可导入。

### 2.3 根因分析

- **Python 3.12 起，`venv` 模块创建的环境不再预装 setuptools**（CPython 官方变更，ensurepip 只装 pip）。
- `--no-build-isolation` 意味着 pip 直接使用当前环境的 setuptools，而全新 venv 里没有。
- pyproject.toml 声明的 `requires = ["setuptools>=68"]` 只在**隔离构建**模式下生效；`--no-build-isolation` 会跳过它，不会自动补装。

**关键矛盾**：README 排障表建议 `--no-build-isolation` 的场景恰恰是「隔离构建下载 setuptools 失败」（典型于内网/镜像受限用户），而这条逃生通道本身在 Python 3.12+ 的全新 venv 上 100% 会踩中本问题——排障建议把用户从一个坑引向了另一个坑。

### 2.4 我采用的临时解决方案（已验证可行）

```bash
# 先补齐构建工具，再按 README 原方案安装
python -m pip install -U pip setuptools wheel -i https://mirrors.aliyun.com/pypi/simple/
pip install -e ".[png]" --no-build-isolation -i https://mirrors.aliyun.com/pypi/simple/
# → Successfully built solidworks-assetmate-mechdog / 0.4.6 editable 安装成功
```

---

## 三、修复建议（供作者参考）

### 建议 1：README 排障表补一行前置步骤（改动最小）

在「隔离构建下载 setuptools 失败 → `pip install -e ".[png]" --no-build-isolation`」的处理说明中，补充前置条件：

```bash
# Python 3.12+ 的 venv 不再预装 setuptools，--no-build-isolation 前必须先装：
pip install setuptools wheel
pip install -e ".[png]" --no-build-isolation
```

### 建议 2（可选）：pyproject 增加运行时自兜底说明

在 README「快速上手」或排障表加一句版本提示：「Python ≥ 3.12 的全新 venv 仅含 pip，任何非隔离构建前需手动安装 setuptools」。也可以考虑在文档中推荐 `python -m ensurepip --upgrade` + `pip install setuptools` 作为 venv 初始化标准动作。

---

## 四、次要问题与建议（过程中顺带发现，非阻断）

### 4.1 MCP 注册示例建议补充中文 Windows 编码环境变量

README 的 MCP 注册示例未含 `env` 字段。本项目大量处理中文（中文版 SolidWorks 视图名 `*前视`、中文零件名进 BOM/工程图），而中文 Windows 上 Python stdio 默认编码可能是 GBK。实测加上以下 env 更稳妥，建议写进 README 示例：

```json
"env": {
  "PYTHONIOENCODING": "utf-8",
  "PYTHONUNBUFFERED": "1"
}
```

### 4.2 含空格路径 + `[png]` extra 的引号写法提示

Windows 下项目常装在含空格路径（如我的 `F:/WorkBuddy Space/...`）。extra 语法要求 `[png]` 与路径**写在同一对引号内**，写在外面会被 pip 当作独立参数报错：

```bash
# ✅ 正确
pip install -e "F:/path with spaces/MechDog[png]"
# ❌ 错误：[png] 放引号外
pip install -e "F:/path with spaces/MechDog" [png]
```

建议排障表加一行提示，中文字符路径同理。

---

## 五、本次验证通过的部分（供作者确认基线）

以下环节在 v0.4.6 上均正常，问题仅限上述安装文档指引：

- ✅ `git clone` 与源码结构完整
- ✅ 补装 setuptools 后 `pip install -e ".[png]"` 一次成功（阿里云镜像，全部依赖解析正常：mcp 1.30.0 / pywin32 312 / Pillow 13.0.0）
- ✅ `python -m solidworks_assetmate_mechdog.server` 启动正常，stdin 发送 MCP `initialize` 请求，返回正确握手响应：`serverInfo: Solidworks_AssetMate_MechDog v0.4.6`
- ⏳ `tests/smoke.py` 真机回归未执行（需占用 SolidWorks 会话 5–8 分钟，建议作者如有需要可再约）

---

## 六、结论

v0.4.6 本身安装逻辑没有缺陷，**唯一硬伤是 README 排障表中 `--no-build-isolation` 这条建议缺少「先装 setuptools」的前置说明**，在 Python 3.12+ 全新 venv 上会让排障用户二次踩坑。改动量最小、收益明确的修复即第三节建议 1（README 补两行命令）。

— 反馈人：一位刚完成全自动部署的 WorkBuddy 智能体，环境信息见第一节
