# Abaqus-Control-MCP — Abaqus 2023 / Python 2.7 兼容移植版

原版 [Whfkl/Abaqus-Control-MCP](https://github.com/Whfkl/Abaqus-Control-MCP) 要求
Abaqus 内嵌 **Python 3**(2024+)。本目录是它的**完整版移植**,让同一套 MCP 工具在
**Abaqus 2023(内嵌 CPython 2.7.15)**上可用,并**完整保留原版的智能错误诊断面板**
(AST 反解析、相似 key/属性推荐、callable 签名提取)。

> ✅ **已在 Abaqus 2023 实机打通**:`cli check` 返回 `python: 2.7.15` + `ok:true`,
> 能读到真实 `Model-1` / `Viewport: 1`。本文件记录了实机踩到的坑(见「实战踩坑」),
> 这些在原版文档里都没有。

---

## 架构:两个 Python,各司其职

```
┌─────────────────────────┐   stdio    ┌──────────────────────────┐  TCP 127.0.0.1:48152  ┌────────────────────────────┐
│  Claude Code / Cursor   │ ─────────> │  MCP server (server.py)  │ <──────────────────── │  Abaqus/CAE 2023 内核        │
│                         │            │  = 系统 Python 3.13       │                       │  = 内嵌 Python 2.7.15        │
└─────────────────────────┘            └──────────────────────────┘                       └────────────────────────────┘
                                       这一侧不改(Py3)                                    这一侧是本移植的重点(Py2.7)
```

- **服务端(server/client/cli/protocol)** —— 跑系统 Python 3.13,**未改动**。
- **内核侧(agent.py / gui_plugin.py / diagnostics.py)** —— 跑 Abaqus 内嵌 2.7,**全部重写为 2.7-safe**。
- **deployable = 子包文件夹**:Abaqus 2023 只加载 `<Name>_<ver>/<Name>_plugin.py + <Name>/`
  形式的插件。`build_plugin_pkg.py` 从上述源码一键生成该子包,`install_plugin.bat` 部署它。

## 目录速览

```
abaqus_link/
├── abaqus_mcp_bridge/          # Python 源码包(单真源)
│   ├── server.py / cli.py ...  #   系统侧 Py3,未改动
│   ├── gui_plugin.py           #   GUI 插件主逻辑(Py2.7-safe;无菜单注册尾段)
│   ├── agent.py                #   无界面 socket 路线(Py2.7-safe)
│   └── diagnostics.py          #   错误诊断真源(双版本 AST 兼容)
├── build_plugin_pkg.py         # 源码 → 可部署子包(每次改源码后跑)
├── install_server.bat          # pip 装服务端(mcp+anyio)
├── install_plugin.bat          # 构建+部署子包到 CAE 插件目录
├── verify_kernel.bat           # 用 abaqus 内嵌 2.7 编译/冒烟验证
└── tests/smoke_diagnostics.py  # 2.7-safe 单测(可被 verify_kernel 调用)
```

---

## 安装步骤

### 1)装服务端(系统 Python 3,需要联网装 mcp+anyio)

你机器没装 uv,直接用 pip。运行:

```bat
install_server.bat
```

等价手动命令(在本目录下):

```bat
python -m pip install --upgrade pip hatchling
python -m pip install -e .
```

> 注意:若误装了 `mcp 2.x`(FastMCP 被改名),server.py 会报
> `No module named 'mcp.server.fastmcp'` → `pip install "mcp<2"` 即可。

### 2)构建并部署 CAE 插件(内核侧 Py2.7)

```bat
install_plugin.bat
```

它:①跑 `build_plugin_pkg.py` 生成 `_pkg_out\AbaqusMcpBridge_1.0.0\`;②删掉插件目录里的旧副本;
③把新子包拷进 `E:\Abaqus2023\Temp\abaqus_plugins\`(**你的 CAE 实际扫描的目录**)。

> 换目录:`set ABAQUS_MCP_PLUGIN_DIR=你的目录 & install_plugin.bat`

### 3)接进 Claude Code(MCP 客户端注册)

```bash
claude mcp add abaqus -- python -m abaqus_mcp_bridge.server
```

重启 Claude 后 `/mcp` 应看到 `abaqus`(✔ Connected)。

---

## 日常使用

1. 打开 Abaqus/CAE → `Plug-ins → Abaqus-Control-MCP → Start MCP Bridge`
   (消息区出现 `Listening on 127.0.0.1:48152`)
2. 普通命令行验证:
   ```bat
   cd /d D:\DataBase\claudecode\cabbage\abaqus_link
   python -m abaqus_mcp_bridge.cli check
   ```
   期望:`ok:true` 且 `python: 2.7.15`。
3. 之后在 Claude 里用工具:`ping` / `run_python` / `set_workdir` /
   `monitor_job_status` / `inspect_odb` / `capture_viewport`。

试一条会故意报错的,看诊断面板是否在 2.7 上工作:

```python
mdb.models['不存在']     # 期望返回 possible_keys 列出真实模型名
```

**改了 gui_plugin.py / diagnostics.py 后**:重跑 `install_plugin.bat` **并重启 CAE**
(插件启动时才加载)。

---

## 实战踩坑(本机实机验证,非推测)

这些是移植+部署过程中真实踩到、已修复的坑,写在这里以免重犯:

1. **插件必须是子包形式,不能是平铺 .py。** Abaqus 2023 会把插件目录里的平铺 `.py`
   **编译成 .pyc 但不执行**——菜单永不出现。必须是
   `E:\Abaqus2023\Temp\abaqus_plugins\AbaqusMcpBridge_1.0.0\` 下放
   `AbaqusMcpBridge_plugin.py` + `AbaqusMcpBridge\` 子包(与你机器上另两个能用的插件一致)。
   检查依据:平铺版在启动时只留下 `.pyc` 且无任何日志;子包版日志出现
   `EARLY import begin` + `menu buttons registered OK`。
2. **`socketserver` 在 Abaqus 2.7 里叫 `SocketServer`。** import 那行直接崩、菜单消失。
   必须 `try: import socketserver except ImportError: import SocketServer as socketserver`。
3. **`sendCommand` 拒绝我们的长多行模板。** 报
   `TypeError: command arg must be a string or sequence of strings`(来自
   `sendcmd.py` 的 `__typeCheckCommands`)。实测:短命令(数百行内、各种引号/base64)全过;
   而含缩进块+多行的模板,哪怕 ~2200 字符也必失败,且与长度/引号/base64 无关。
   **解法:把整段脚本写到临时 `.py`,sendCommand 只发 `execfile('...')` 一条极短命令。**
   (这也顺带消除了所有长度顾虑。)
4. **ping 代码里 `os.cpu_count()` 是 py3.4+ 才有**,2.7 上直接 AttributeError →
   内核报错 → CLI 显示 `FAILED: 'return_value'`。已改为 try/except 兜底返回 None。
5. **内核错误要透传**:GUI 侧吞掉 traceback 会把错误压成裸 key,难排查。现已在超时
   后兜底返回残余 response,并记录完整 traceback 到插件日志。

---

## 相比原版改了什么(Python 2.7 兼容点)

**gui_plugin.py 外层**(被 2.7 加载时执行):
- `import queue` → `try: import Queue as _queue_mod except ImportError: import queue as _queue_mod`
- `raise TimeoutError(...)` → 2.7 无此内置,补了 class fallback
- `isinstance(code, str)` → `string_types`(2.7=`basestring`)
- **sendCommand 长模板改为文件 execfile 路线**(见踩坑 3)
- 错误诊断不再整段内嵌,而是 `_load_diagnostics_source` 读同目录 diagnostics + 落盘 staging

**diagnostics.py(新增,诊断真源)**:
- AST 层同时认 Py3 的 `ast.Constant` 和 Py2 的 `ast.Str/Num/NameConstant`(`getattr(ast,...)` 守护,缺失不炸)
- Subscript slice 自动拆 Py2.7 的 `ast.Index` 包裹(`unwrap_slice`)
- **`ast.unparse` 回退**:2.7 没有该函数 → 按节点 `lineno/col_offset` 从源码切片还原表达式原文(已在 py3 上逐字符比对等于 ast.unparse)
- `_extract_tb_lineno`:2.7 异常对象无 `__traceback__` → 走 `sys.exc_info()[2]`
- `redirect_stdout/stderr`:2.7 无 `contextlib.redirect_*` → 手写 `_redirect` 上下文管理器
- 去 f-string / 去类型注解 / 去 PEP604 联合 / 去内建泛型

**server.py / client.py / cli.py / protocol.py**:未改动(系统 Python 3.13)。

---

## 已知边界

- 极端嵌套调用(如 lambda 内再抛 TypeError)的 `call_target` 可能取不到——原版同样局限,不影响其余三类(KeyError/AttributeError/NameError)诊断。
- 服务端 `pip install` 需联网;离线环境请先在有网机器装好再拷 site-packages。
- 诊断/恢复逻辑在 2.7 上通过 `cli check` 的成功路径与冒烟脚本验证;错误路径的「智能推荐」仍建议后续在 CAE 里用 `mdb.models['不存在']` 这类用例实测一次。
- **同一插件别同时放两份**(Abaqus 会同时扫 `C:\Users\...\abaqus_plugins` 和 `E:\Abaqus2023\Temp\abaqus_plugins`),会弹 Duplicate warning 并可能串版本——只留一份。
