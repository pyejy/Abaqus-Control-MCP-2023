# Abaqus-Control-MCP · Abaqus 2023 / Python 2.7 移植版

> **本目录 (`abaqus_mcp_pro`) 是在移植版基础上做过一轮工程整改的独立分支**：新增
> `get_model_info` / `submit_job` / `read_odb_field` / `read_odb_history` / `extract_kpis`
> 五个工具，并修掉了 run_python 大返回值撑爆响应的隐患。详见 **[IMPROVEMENTS.md](IMPROVEMENTS.md)**。
> 建模/后处理的 API 踩坑速查见 **[skills/abaqus-mcp-workflow/SKILL.md](skills/abaqus-mcp-workflow/SKILL.md)**。

> 本地 MCP（Model Context Protocol）桥接器：让你在 **Claude Code / Cursor** 等 MCP 客户端里实时操控 **Abaqus/CAE** —— 发 Python 脚本、读 ODB 结果、看视口截图，全程无需手动点 GUI。
>
> **原参考项目（本仓库为它的 Abaqus-2023 兼容移植）：**
> [Whfkl/Abaqus-Control-MCP](https://github.com/Whfkl/Abaqus-Control-MCP)（MIT）— 原版要求 Abaqus 2024+（内嵌 Python 3）。
> 本仓库把它完整移植到 **Abaqus 2023（内嵌 CPython 2.7.15）**，保留原版智能错误诊断面板。
>
> 详细移植记录与实战踩坑见 **[README_2023.md](README_2023.md)**。

---

## 它解决什么问题

Abaqus/CAE 自带一个 Python 内核，但普通脚本只能在 Abaqus 里跑、看不到结果图；原版桥接器通过 MCP 把内核"露"给 LLM，让 Claude 能像操作本地 Python 一样操作你的 CAE 会话：

```
┌─────────────────────────┐  stdio    ┌──────────────────────────┐  TCP 127.0.0.1:48152  ┌────────────────────────────┐
│  Claude Code / Cursor   │ ─────────> │  MCP server (server.py)  │ <──────────────────── │  Abaqus/CAE 2023 内核        │
│                         │            │  = 系统 Python 3          │                       │  = 内嵌 Python 2.7.15        │
└─────────────────────────┘            └──────────────────────────┘                       └────────────────────────────┘
                                       这一侧 = 原版(未改)                                  这一侧 = 本移植的重点(Py2.7)
```

## 与本仓库配套的工具（全部可用）

- `ping` — 探活，返回内核 Python 版本 / 模型 / 视口清单
- `run_python` — 在 Abaqus 内核里执行任意 Python，捕获 stdout/stderr + 返回值，出错时给**智能诊断**（含可能 key / 属性 / callable 签名建议）
- `set_workdir` / `monitor_job_status` / `inspect_odb` / `capture_viewport`
- 另配一批 Claude Code 技能（`abaqus-control-check` / `abaqus-control-setup` / `abaqus-control-doctor`）

---

## 目录速览

```
abaqus_link/
├── abaqus_mcp_bridge/          # Python 源码包(单真源)
│   ├── server.py / cli.py ...  #   系统侧 Python3,未改动
│   ├── gui_plugin.py           #   GUI 插件主逻辑(Py2.7-safe)
│   ├── agent.py                #   无界面 socket 路线(Py2.7-safe)
│   └── diagnostics.py          #   错误诊断真源(双版本 AST 兼容)
├── build_plugin_pkg.py         # 源码 → 可部署插件子包
├── install_plugin.bat          # 构建 + 部署插件到 CAE 插件目录
├── install_server.bat          # 安装服务端(mcp + anyio)
├── verify_kernel.bat           # 用 Abaqus 内嵌 2.7 编译 / 冒烟验证
├── tests/smoke_diagnostics.py  # 2.7-safe 单测
├── pyproject.toml              # 服务端打包(mcp/anyio)
├── README.md                   # 本文件(交付总览)
└── README_2023.md              # 详细移植记录 + 实战踩坑
```

## 快速开始（三件套）

**① 装服务端（系统 Python 3）**
```bat
install_server.bat
```

**② 构建并部署 CAE 插件（内核侧 Py2.7）**
```bat
install_plugin.bat
```
> 改任何 `gui_plugin.py` / `diagnostics.py` 后**重跑**本命令并**重启 CAE** 才生效。

**③ 接进 Claude Code**
```bash
claude mcp add abaqus -- python -m abaqus_mcp_bridge.server
```
重启 Claude 后 `/mcp` 应看到 `abaqus` ✔ Connected；然后打开 Abaqus/CAE → `Plug-ins → Abaqus-Control-MCP → Start MCP Bridge`。

> 换插件安装目录：`set ABAQUS_MCP_PLUGIN_DIR=你的目录 & install_plugin.bat`

## 验证是否真的通了

```bash
cd abaqus_link
python -m abaqus_mcp_bridge.cli check      # 期望 ok:true + python: 2.7.15
```

在 Claude 里试一条会故意报错的，看 2.7 上的智能诊断是否工作：

```python
mdb.models['不存在']     # 期望 possible_keys 列出真实模型名
```

## 一个真实可跑的端到端例子

本仓库用**悬臂梁（Cantilever）静力分析**做过完整端到端回归：建模 → 网格(C3D8R) → 固支 + 自由端节点集中力 → 提交 → 收敛 → ODB 读位移。期间修掉了两个非本仓库根因的环境 bug：

1. **Abaqus 内核劫持 `sys.stdout` 为只收 unicode 的 `io.StringIO`** → 任何含 `print` 的代码都报 `TypeError: unicode argument expected, got 'str'`（且连 `print(u'中文')` 都炸——Abaqus 把 print 的写路径也 byte 化了）。修法：容错流 + 启动探针（详见 §4/§5 of README_2023.md）。
2. **Load 的 region 指向部件级面集合而装配是 dependent 实例** → 报 `region contains invalid geometry or mesh components for the load type`。修法：改用装配级节点集合承载集中力。

这些都**已在 Abaqus 2023 实机验证**。若你要跑自己的作业，建议让 `run_python` 里所有 `print` 只含 ASCII；含中文的 print 在 2023 内核会有 ascii 编码限制（本仓库已给容错流加 repr 兜底，重启后即彻底无感）。

---

## 许可与归属

- 代码：MIT License（见 [LICENSE](LICENSE)；上游版权声明保留，属本项目对
  [Whfkl/Abaqus-Control-MCP](https://github.com/Whfkl/Abaqus-Control-MCP) 的移植扩展）。
- 请保留对上游的致谢与本文件的来源说明。
