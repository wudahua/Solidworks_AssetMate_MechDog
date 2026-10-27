# MCP 服务器侧踩坑（非 SolidWorks）

> SolidWorks COM 的坑见 [com-quirks.md](com-quirks.md)。这里只记 MCP SDK / 安装相关。

## 1. `serverInfo.version` 默认不是本包版本

`FastMCP(name)` 在 mcp 1.x 没有 `version` 参数，低层 Server 会退回到 **mcp 包的版本**：

```json
"serverInfo": { "name": "Solidworks_AssetMate_MechDog", "version": "1.30.0" }
```

结果就是客户端无法从握手判断工具集版本。修复（2026-10-27，反馈 #v0.1.3）：

```python
mcp = FastMCP(SERVER_NAME)
if getattr(mcp, "_mcp_server", None) is not None:
    mcp._mcp_server.version = __version__
```

低层 `Server.create_initialization_options()` 用 `self.version or pkg_version("mcp")`，所以赋值即可。
这是私有属性，升级 `mcp` 大版本时要复查；本项目已 pin `mcp<2`。

验证方式：

```python
from solidworks_assetmate_mechdog.server import mcp
print(mcp._mcp_server.create_initialization_options().server_version)  # 0.1.4
```

## 2. 注册命令优先用 `python -m`

`pip install` 的入口装在 `<python>\Scripts\solidworks-assetmate-mechdog.exe`，该目录默认不在 PATH。
MCP 客户端报「找不到命令」时先怀疑这里。用 `python -m solidworks_assetmate_mechdog.server` 不依赖 PATH，多环境时把 `python` 换成绝对路径。

## 3. 构建隔离与国内镜像

- pip 默认创建隔离构建环境并重新下载 `setuptools`；弱网会失败 → `--no-build-isolation`。
- 部分国内镜像对 pip 的 HTTP 栈返回 403，而 `curl`/`urllib` 正常；报错常只显示 `from versions: none`，看不出是 403 → 换阿里云源可解。

这两条属于用户环境问题，不是代码缺陷，但 README 必须写明，否则「照文档装不上」。
