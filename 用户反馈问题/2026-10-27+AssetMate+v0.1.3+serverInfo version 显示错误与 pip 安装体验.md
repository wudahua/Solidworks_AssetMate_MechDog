# 2026-10-27 AssetMate v0.1.3 安装反馈：serverInfo.version 显示错误 + pip 安装体验问题

> 反馈人环境：Windows 10 x64 · SolidWorks 2026 SP0（RevisionNumber 34.0.0，安装时正在运行）· Python 3.13.12 (64-bit) · WorkBuddy MCP 客户端
> 安装对象：`wudahua/Solidworks_AssetMate_MechDog` @ main（8fab0c2），`pip install -e ".[png]"` 可编辑模式
> 结果：**安装与运行全部成功**（stdio 握手通过，8 个工具全部注册，COM attach 成功），但过程中发现以下问题，建议作者优化。

---

## 核心问题：MCP 握手返回的 `serverInfo.version` 不是包版本，而是 mcp 库版本

### 现象

`initialize` 握手返回：

```json
"serverInfo": { "name": "Solidworks_AssetMate_MechDog", "version": "1.30.0" }
```

`1.30.0` 是当时环境中 `mcp` 库的版本，**不是 AssetMate 自己的版本 0.1.3**。客户端无法从握手中得知所连接的 MCP 工具集版本，升级/降级后也无法用 `serverInfo` 区分——对多机器部署与问题排查不友好。

### 复现

任意 stdio 客户端发送 initialize 后读取 `result.serverInfo.version`，观察到的是 FastMCP 默认回退值（mcp 库版本），随环境变化。

### 根因

`src/solidworks_assetmate_mechdog/server.py` 中：

```python
mcp = FastMCP(SERVER_NAME)          # 未传 version
```

而包版本已在 `__init__.py` 定义（`__version__ = "0.1.3"`）却未被使用。

### 建议修复（一行）

```python
from . import SERVER_NAME, __version__
mcp = FastMCP(SERVER_NAME, version=__version__)
```

修好后 `serverInfo.version` 将稳定返回 `0.1.3`，与 `pyproject.toml` 一致，后续发版即可通过握手识别版本。

---

## 次要问题：安装体验（pip 阶段，3 条）

以下均为环境/生态侧问题，非代码缺陷，但直接影响 README 安装命令的开箱成功率，供文档与打包参考。

### 2.1 `pip install .` 默认 build isolation 会额外联网拉 setuptools，弱网环境易失败

`pyproject.toml` 声明 `requires = ["setuptools>=68"]`，pip 默认为构建创建隔离环境并**重新下载 setuptools**。本次隔离环境下载失败（镜像 403，见 2.2），必须改用：

```bash
pip install -e ".[png]" --no-build-isolation   # 前提：主环境已装 setuptools
```

建议：在 README 安装一节注明该回退方案；或考虑把 `setuptools` 从 build-requires 依赖链的影响降到最低（如提供 `py_modules` 式零构建安装说明，因 src 布局极简）。

### 2.2 清华 PyPI 镜像对 pip 的网络栈返回 403（同机 urllib / curl 均 200）

诊断摘要（同一台机器、同一时刻）：

| 访问方式 | 清华镜像 | pypi.org | 阿里云镜像 |
|---|---|---|---|
| `curl` | 200 | 200 | — |
| Python `urllib`（含 pip 同款 UA/PEP 691 Accept 头复现） | 200 | 200 | — |
| `pip` | **403 Forbidden** | **403**（同型报错） | **200，安装成功** |

- 已排除：系统代理（ProxyEnable=0）、环境变量代理（`env -u` 全清后依旧）、pip 配置文件（四处 pip.ini 均不存在）。
- 表现为 pip 自带 HTTP 客户端（vendored requests/urllib3 栈）与清华镜像之间的特征性 403，换阿里云镜像立即成功。
- 建议：README「安装」一节可加一行备注——国内网络下若镜像 403，可改用 `pip install -i https://mirrors.aliyun.com/pypi/simple/ ...`；这类问题用户侧极难自查（报错只显示 `from versions: none`，完全看不出是 403）。

### 2.3 `Scripts` 目录不在 PATH，README 的裸命令注册方式会启动失败

`pip install` 后可执行入口装在 `<python>\Scripts\solidworks-assetmate-mechdog.exe`，该目录通常不在 PATH。README 给出的注册写法：

```json
"command": "solidworks-assetmate-mechdog"
```

在默认 PATH 下客户端会直接「找不到命令」。README 已给出 `python -m` 的开发模式写法（本次即采用该方式，工作正常），建议把它升格为**默认推荐**，裸命令标注"仅当 Scripts 在 PATH 时可用"。

---

## 本次验证记录（供作者参考，全部通过）

| 验证项 | 结果 |
|---|---|
| stdio JSON-RPC initialize → initialized → tools/list | OK，protocol 2024-11-05 |
| 工具注册数 | 8/8（recolor_assembly、list_bodies、recolor_bodies、export_bom、rename_components、get_assembly_tree、get_mass_properties、export_preview） |
| serverInfo.name | `Solidworks_AssetMate_MechDog` ✅（仅 version 错，见核心问题） |
| COM attach（`GetActiveObject`，SolidWorks 运行中） | OK，RevisionNumber 34.0.0 |
| 依赖组合 | mcp 1.30.0 + pywin32 312 + Pillow 13.0.0 + Python 3.13.12 x64 |

---

*以上基于 main 分支 8fab0c2 实测。所有诊断过程有原始日志，如需 403 的 pip -vvv 完整输出可联系反馈人。*
