# abaqus_mcp_pro — 相对上游的整改记录

本目录是 `abaqus_link`（= `pyejy/Abaqus-Control-MCP-2023`，Whfkl 原版的 2023/Py2.7 移植）
的**独立改进分支**。原版只有 6 个工具且逐字等于上游；本次把它从「忠实移植」推进到
「能用的工程助手」。**原 `abaqus_link/` 未被改动。**

## 新增 / 修改

### P0 · 返回值体量保护 — `diagnostics.py`（内核侧, Py2.7-safe）
- 症状：stdout/stderr 截到 1000 字符，但 `result` 走 `jsonable()` **原样序列化**，
  脚本返回大数组会灌满 MCP 响应并撑爆 protocol.py 的 16 MiB socket 上限。
- 修法：新增 `safe_return_value()` + `_downsample_list()` + `_json_size()`。
  - 小值原样透传（行为不变）。
  - list/tuple 超阈值 → `{__truncated__, kind, total, returned, dropped, size_bytes, sample, hint}`
    （head 采样，≤200 项且 ≤8 KB）。
  - dict 不静默截断（会隐藏 key 存在性），仅标记 + preview。
  - 不可序列化对象仍走旧 `{repr,type}`。
  - 兜底：preview 仍过大时降级为最小错误信封。
  - 阈值可用内核环境变量 `ABAQUS_MCP_RETURN_MAX` 覆盖（默认 60000 字节）。
- ⚠️ 改了 diagnostics.py → **需重跑 `install_plugin.bat` 并重启 CAE** 才生效。

### P1 · 新工具 — `server.py`（系统 Python 3 侧, 无需重编插件）
全部基于既有 `_exec()` 拼内核代码实现，不改 socket 协议：
- `get_model_info(model_name?, detail)` — 一次性反射 parts/instances/materials/sections/
  steps/loads/bcs/interactions/named sets+surfaces/mesh/jobs/amplitudes。summary 轻量、full 加类型。
- `submit_job(job_name, model_name?, cpus?, domains?, double_precision?, blocking=False)` —
  建作业+提交，**默认非阻塞**避免冻住 GUI 线程；配合 monitor_job_status 轮询。blocking=True 读 .sta 尾。
- `read_odb_field(...)` — region/component/invariant 选择 + offset/limit 分页 + `export_csv` 落盘回路径。
- `read_odb_history(series, ...)` — 单条历史曲线，自动等距降采样到 ≤limit_points。
- `extract_kpis(odb_path, queries=[...])` — 声明式配方，服务端归约成**标量**字典；LLM 永不见原始数组。
  field 支持 invariant(Mises/MaxPrincipal/…)+region+reducer(max/min/mean/sum/first/last/count)；history 按 series 子串匹配。
- 共享内核 helper `_ODB_HELPERS`（区域解析 + 取值归约），注意其内部函数用单引号 docstring
  ——因为整段被塞进三引号 r-string，内层不能用 `"""`。

### P3 · 工作流技能 — `skills/abaqus-mcp-workflow/SKILL.md`
把生态里反复踩的 Abaqus API/2.7 坑固化成速查（区域引用、setFrame 整型索引、张量 refinement、
print ASCII、.lck、非阻塞提交、token 纪律）。作为交付物放项目内；装到 `~/.claude/skills/` 即全局可用。

### INSTRUCTIONS（server.py）
新增「优先工具 > 裸 run_python」段落与「结果体量规则」，让 agent 主动用 get_model_info /
submit_job / extract_kpis，而不是每次手写样板再撞 token 墙。

## 验证状态
- ✅ `py_compile` server.py / diagnostics.py 通过（Python 3.13）。
- ✅ 9 段内嵌内核 snippet + 真实代入参数后 `ast.parse` 全过，无残留占位符。
- ✅ return-size guard 冒烟：int-list 688KB→信封 1187B；tuple/dict/unserializable 各路径正确。
- ✅ FastMCP 注册表枚举到 11 个工具（原 6），schema 正常加载。
- ✅ **实机回归（Abaqus 2023 内核, 2026-09-09）已全部跑通**，见下「实测暴露的 Abaqus API 差异」。
- ⬜ Py2.7 编译验证：跑 `verify_kernel.bat`（本机无 system python2，靠 Abaqus 内嵌解释器）。

## 实测暴露的 Abaqus API 差异（真机修掉，离线猜不到）
真机回归（Cantilever / Cylinder-SimplySupported 两个 ODB + submit_job）期间，暴露并修复了
三处「只在 Abaqus 2023 真机上才会报」的差异：

1. **`mdb.models[name]` 没有 `.fields` 属性**（Model 对象没有 fields 容器，只有
   `.fieldOutputRequests` / `.predefinedFields`）。→ get_model_info 改为
   `fields=[]` + `predefined_fields=[]`，空容器用 `_safe` 兜底。
2. **ODB 的 `FieldValue` 没有 `.label`**（displacement/stress 等没有 label，只有
   `.nodeLabel` / `.elementLabel`）。原始实现用 `.label` 作行标签 → 任何
   read_odb_field 都炸。→ 新增 `_label_of(vv, fallback)` 先 nodeLabel 再 elementLabel。
   **注意 `.values` 不能切片**（`sub.values[:10]` 报 "Slice … not supported"），须索引循环。
3. **Abaqus 2023 的 `Job.submit()` 签名只有 (consistencyChecking, datacheckJob, continueJob)**，
   不接受 `numCpus/numDomains/doublePrecision`。→ 并行/精度参数先 `job.setValues(**kw)` 再
   无参 `job.submit()`。且 **numDomains 必须是 numCpus 的倍数**（域级并行），否则报
   "Number of domains must be a multiple of the number of processors"。

其它真机事实（对后续做 ODB 工具有用）：
- 位移 U / RF 反力是 **NODAL** 数据，`fieldOutputs.getSubset(region=rootAssembly.nodeSets['X'])`
  能直接取到节点集上的值；应力 S 是 **INTEGRATION_POINT** 数据，node_set 的 getSubset 返回 0 值，
  须用 `instance.elementSets['ALLCELLS2']`（全单元集）才取得到 → extract_kpis/read_odb_field 里
  S + node_set 会得到 null，属预期，文档里已提示 region_type=el_set。
- Abaqus 历史输出在 `odb.steps[s].historyRegions[region].historyOutputs[key].data`（region 名形如
  `Node CYLINDER-1.15`、`Assembly ASSEMBLY`），series 匹配用子串即可。
- 已存在的作业重复 submit 报 "The job is running and cannot be re-submitted" —— 若返回
  created_now=false 且用户想重跑，应先确认 .sta 已 COMPLETED 或显式 kill。
- 从内核直接 `import abaqus_mcp_bridge` 拿不到系统侧包（Abaqus 内核 sys.path 是它自己的
  2.7 环境）；若要跑系统侧新代码，须手动 `sys.path.insert(0, <pro 目录>)` 后导入
  `diagnostics` 再 exec 目标 snippet（本次实测即如此验证 P0 / 各工具逻辑）。

## 尚未做（调研清单里的 Tier 2/3，按需再加）
- `check_silent_failures`（收敛但答案错的检测）、`diagnose_job` 正则库增强、typed 建模快捷工具、
  视口云图 `capture_result_contours`、capsule/diff 回归 QA 层、unit-system helper。
