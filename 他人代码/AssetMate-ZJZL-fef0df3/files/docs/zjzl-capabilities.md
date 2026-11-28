# ZJZL 工程图与装配工具

ZJZL 使用 AssetMate 现有的 MCP 服务和 SolidWorks 会话。通过 `list_tool_profiles` 查看工具，通过 `check_capabilities(operation=工具名)` 查询各项适用条件。

## 工程图操作

| MCP 工具 | 使用方式 |
| --- | --- |
| `zjzl_create_orthographic_views` | 给出现有父视图名称和 `top`、`bottom`、`left`、`right` 中的任意组合。第一角法把上视放在下方、右视放在左侧，第三角法方向相反。用 `offset_m` 指定中心距。工具核对原生父子关系、方向、配置、比例与轮廓。 |
| `zjzl_create_custom_view` | 传入零件或装配的绝对路径、观察法向 `normal`、向上提示 `up` 和图纸坐标 `position_m`。例如 `normal=[0.6,0,0.8]`、`up=[0,1,0]`。可指定模型配置。工具恢复模型视口并删除临时方向名，源模型不执行保存。 |
| `zjzl_probe_geometry` | 在指定图纸视图读取直线边、圆弧、圆、其他可读曲线及可见顶点。模型与纸面坐标都用米。装配可用 `component` 检索一个实例。输出 `issues` 记录轮廓、曲线或顶点中无法完整读取的内容。 |
| `zjzl_add_reference_dimension` | 将探测得到的 `reference` 对象完整传入 `references`。线性尺寸用两点或一条直边，角度用两条直边，半径和直径用一个正对视图的圆或圆弧。尺寸以带括号的原生从动标注保存。回执含尺寸持久 ID、关联和实际值；可传 `expected_value` 和 `tolerance` 核对，长度用毫米、角度用度。 |
| `zjzl_inspect_mates` | 传入 `.SLDASM` 绝对路径。读取活动配置下当前装配层级的配合名称、原生类型、对齐、抑制及引用，距离和角度配合有可读取时返回数值。对子装配文件可单独调用。 |
| `zjzl_add_bom_balloon` | 先插入原生 BOM，再探测装配视图的可见组件边。传入一个该边的 `reference` 与气泡位置。工具优先使用视图明确关联的 BOM，否则查找同页唯一匹配表。用配置和实例身份核对唯一可见行，可用 `expected_item` 核对件号。 |

工程图参数使用绝对路径与准确的视图名称。省略 `sheet` 时使用活动页。写入工具默认 `save=false`，成功后可先在 SolidWorks 检查内存结果。传 `save=true` 时保存整份工程图，包括其中其他尚未保存的编辑。请用样件副本做验收。

同一视图的引用可以在保存重开后尝试解析。引用仍受图纸路径、页、视图、装配实例和可见性约束。复制或改名图纸、切换配置、重建引起拓扑变化后，重新探测目标几何。轮廓线、卸载模型以及部分剪裁视图可能无法提供可选实体。投影布局校验使用视图包围轮廓，标题栏及其他图纸注记需要目视验收。BOM 分割表或其他无法解析为唯一表和行的布局会被拒绝。轻化、抑制或损坏的 Mate 引用可能只返回部分字段。

## 验收

在 AssetMate 仓库执行离线检查。真机测试需 Windows、运行中的 SolidWorks、可用许可证和对应的本机图纸及 BOM 模板。使用第一角法 A3 模板；测试零件为正视图有直边和 Ø5 圆孔的 60 × 40 mm 板件。装配样件使用同一零件的两个实例，带一个 20 mm 距离配合，按顶层 BOM 合为一个件号。测试复制文件并重绑装配引用，仅向新建的测试目录保存。可分开提供零件和装配参数。

```powershell
& .\.venv\Scripts\python.exe -X utf8 -B tests\check_all.py
& .\.venv\Scripts\python.exe -X utf8 -B tests\zjzl_live.py `
  --part 'C:\CAD\Plate.SLDPRT' `
  --assembly 'C:\CAD\GapAssembly.SLDASM' `
  --component-part 'C:\CAD\MatePlate.SLDPRT' `
  --drawing-template 'C:\CAD\gb_a3.drwdot' `
  --bom-template 'C:\CAD\bom-standard.sldbomtbt'
```

零件验收检查第一角法上视和右视、重叠撤回、任意方向与模型哈希、跨视图引用拦截、水平尺寸与孔径。装配验收检查距离配合和实例身份、缺 BOM 或件号不符时的拒绝，以及原生件号气泡的保存重开。跨页 BOM、第三角法、其他尺寸类型及 SolidWorks 其他版本应使用相应样件扩展验收。
