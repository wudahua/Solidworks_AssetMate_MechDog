BOM 明细栏资料包
================

请先读：BOM资料包_00_请先读我.md

目录
----
00  BOM资料包_00_请先读我.md      现状、目标、卡点、需要判断的三件事
成功案例/
    BOM_TEST.SLDDRW              宏插入成功的图纸（10 行 4 列，可直接打开对照）
宏/
    insert_bom.swp               界面录制的宏（能成功插入 BOM）
代码/
    layout.py                    当前 add_bom_table 实现（含注释）
    sw_session.py                call_method 统一封装（属性派发陷阱）
脚本/
    01_最简可复现_能创建表格.py   已验证可创建（rows=1 / cols=3）
    02_诊断_列数与配置.py         打印 ColumnCount / TotalColumnCount / 配置状态
文档/
    14 份历史记录（按时间顺序，可跳过）

核心事实（一句话）
------------------
表格能创建，但只有表头：RowCount=1、ColumnCount=3，
而 TotalColumnCount=5 —— 说明「数量列」存在于定义中，只是没渲染。

最关键的两个参数（此前错了，导致误判为「API 不可用」）
------------------------------------------------------
AnchorType 必须 = 1（TopLeft），传 0 直接返回 None
BomType    必须 = 2（TopLevelOnly），1 是 PartsOnly

需要判断的问题
--------------
TotalColumnCount=5 但 ColumnCount=3，是
  A) 模板定义列 vs 实际渲染列
  B) 隐藏列但索引错位
  C) ColumnHidden 是 1-based
这决定下一步往哪走。
