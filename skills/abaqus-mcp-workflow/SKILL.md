---
name: abaqus-mcp-workflow
description: >
  Abaqus/CAE 通过 MCP 桥接建模与后处理的标准工作流与 API 踩坑速查。
  当用 run_python / get_model_info / submit_job / extract_kpis 等工具操作 Abaqus 时使用，
  避免重复犯已知的 Python-2.7 内核与 CAE API 错误（区域引用、setFrame、张量不变量、
  大数组返回、作业阻塞等）。适配 Abaqus 2023 / 内嵌 CPython 2.7.15 移植版。
read_when:
  - 在 Abaqus MCP 里建模型、划网格、加约束载荷
  - 提交求解作业或轮询其状态
  - 从 ODB 读取位移/应力/反力等结果数值
  - run_python 报 KeyError/AttributeError/TypeError 需要定位
  - 视口截图、画云图做报告
---

# Abaqus MCP 工作流与踩坑速查

本技能配合 `abaqus` MCP server（ping / run_python / set_workdir / monitor_job_status /
get_model_info / submit_job / inspect_odb / read_odb_field / read_odb_history /
extract_kpis / capture_viewport）使用。目标是让 LLM **少写样板、少犯错、不爆 token**。

## 0. 黄金流程（每步先反射，再动作）

1. `ping` 确认桥接活着（`python: 2.7.15`）。不通 → 让用户在 CAE 里
   `Plug-ins → Abaqus-Control-MCP → Start MCP Bridge`。
2. `set_workdir` 到干净目录，**先于任何建模**。别让求解文件落在默认路径。
3. `get_model_info()` 看当前模型有什么（parts / sets / steps / loads / jobs），
   别凭记忆猜名字——名字不对是最高频的 KeyError 来源。
4. 分阶段建模（几何+命名集 → 装配/step/网格 → 物理/载荷/提交），**每阶段跑一条
   `get_model_info(detail='full')` 或针对性反射验证**再继续。别一个巨型脚本全包。
5. 提交：`submit_job(name, cpus=N)`（非阻塞）→ `monitor_job_status(name)` 轮询 `.sta`。
6. 读结果：**一律用 `extract_kpis`**（标量）或 `read_odb_field(export_csv=…)`（数组落盘），
   绝不在 run_python 里循环 `field.values` 塞进 `result`。

## 1. 区域引用（最高频 bug 类别）

- Dependent 实例的 Load/BC **不能**指向 part 级面集合，会报
  `region contains invalid geometry or mesh components for the load type`。
  → 集中力用**装配级节点集合**承载；均布/压力用装配级 Surface。
- 取几何优先语义化：`getByBoundingBox(xMin,yMin,zMin,xMax,yMax,zMax)`、
  `getByBoundingCylinder(...)`、按拓扑过滤 `part.faces[:]`。拿到后**立刻**包成
  命名 Set/Surface，后续全部按名字引用。
- 客户端/服务端写入区域时：`assembly.Set(nodes=mesh.MeshNodeArray(...))` —— 传的是
  MeshNodeArray，不是裸 python list。

## 2. Python 2.7 内核硬限制

- 内核劫持 `sys.stdout` 为只收 unicode 的流：`print '中文'` 也可能炸。
  → run_python 里 **print 只用 ASCII**；要回数据用 `result = {...}` 而非 print。
- 没有 f-string、没有 `os.cpu_count()`(3.4+)、没有 `contextlib.redirect_stdout`(3.4+)。
  插件侧已兜底，但你手写代码别用这些。
- 顶层表达式可 `eval`；多行用 exec 并把返回值放进变量 `result`。

## 3. ODB 读取（visualization / odbAccess）

- 选帧：`vp.setFrame(step=si, frame=nf-1)` —— step/frame 必须**整型索引**，不是对象。
- 张量主变量要不变量才能出标量云图：
  `vp.setPrimaryVariable('S', refinement=(INVARIANT, 'Mises'))`
  （关键：`refinement=(INVARIANT,'Mises'|'MaxPrincipal'|'Intensity')`；不带 refinement
  时 S 会按 6 个分量画，无法直接读单一数值）。
- 抽数值走 `extract_kpis`：一条查询 `{field:'S', invariant:'Mises', region_type:'node_set',
  region_name:'NSET-TIP', reducer:'max'}` → 直接得最大 Mises，不传整个应力场。
- 真要大数组：`read_odb_field(..., export_csv='D:/out/U.csv')` 返回 `{path, rows, columns, preview_head}`。
- 打开 ODB 前若报锁定：残留 `.lck` 文件阻止 openOdb → 删 `<job>.lck` 或确认无其它进程占用。

## 4. 作业与并行

- 长分析**不要** `job.waitForCompletion()`（阻塞 CAE GUI 线程，桥接可能超时假死）。
  用 `submit_job(blocking=False)` + 轮询。只有几秒的小算例才 `blocking=True`。
- 并行：`cpus=` → numCpus；`domains=` → numDomains（parallel DIRECT）。
- 判成败看 `.sta` 末尾是否 `Analysis has been completed successfully`，
  并扫 `.msg` 的 `***ERROR` / `***WARNING`（monitor_job_status 已 tail）。

## 5. 常见异常 → 修法（run_python 诊断面板也会给候选）

| 现象 | 根因 | 修法 |
|---|---|---|
| `KeyError: 'Set-1'` | 集合名不存在 | 先 `get_model_info` 列真实 keys；用返回的 Similar 候选 |
| `region contains invalid geometry` | dep 实例引用 part 级面 | 改装配级节点集/面 |
| `print` 报 `unicode argument expected` | 内核 stdout 被劫持 | print 仅 ASCII，数据走 result |
| `TypeError … keyword …` | API 关键字拼错 | 看诊断 Expected Signature / Similar Keywords |
| `ConcentratedPoreFluid(cf=…)` 报错 | 该类用 `magnitude=` 非 `cf=` | 换参数名 |
| 视口截图空白 | 未刷新/朝向丢失 | `vp.restore()` 后再 `capture_viewport`；ASCII 路径更稳 |
| MCP 响应像“旧结果” | 内核阻塞未回 | Stop & Start MCP Bridge；改用非阻塞提交 |

## 6. Token 纪律

- run_python 返回超 ~60KB 会被自动降采样为 `{__truncated__, total, sample}`。
  看到 `__truncated__:true` 说明你取了太多 → 改成取标量/KPI 或落盘 CSV 再读路径。
- 列表类尽量带 `offset/limit`；inspect_odb 已对 frames 做 ≤5 抽样，照此办理。
- 报告数字优先 `extract_kpis` 一次多条查询，别一条条问。
