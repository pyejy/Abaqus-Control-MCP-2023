# 插件装到哪里：按本机的插件目录来

`install_plugin.bat` 只需要知道**你的 Abaqus/CAE 实际会扫描哪个 `abaqus_plugins` 目录**。
不同机器、不同安装方式下这个位置不一样，**没有通用的固定路径**——所以请按本机情况来定。

## 最省事的做法：让 AI 自己找

如果你在用 Claude Code / Cursor 之类的 AI 助手，直接把这句话丢给它：

> 帮我把这个仓库的 Abaqus 插件装好。先确定我这台机器上 Abaqus/CAE 实际扫描的
> `abaqus_plugins` 目录，再设 `ABAQUS_MCP_PLUGIN_DIR` 指向它，然后跑 `install_plugin.bat`。

AI 会自己去找目录。判断依据见下节，它照着查就行。

## 自己找的话：看这几处

Abaqus/CAE 启动时按官方规则扫描下列位置（含其所有子目录），装到任一处都能被加载：

| 优先级 | 位置 | 说明 |
|---|---|---|
| 1 | `plugin_central_dir` | 由 `abaqus_v6.env` / `custom_v6.env` 指定，多用户共享时用这个 |
| 2 | `%USERPROFILE%\abaqus_plugins` | **官方推荐的外部插件位置**，不需要管理员权限，跟工作目录无关 |
| 3 | `<当前工作目录>\abaqus_plugins` | 仅当 CAE 从该目录启动时生效（临时/项目级） |
| 4 | `abaqus_dir\abaqus_plugins` | **不要用**，官方明确不建议往安装目录里塞外部插件 |

### 三个快速排查法

**A. 看你现有的插件在哪**
如果 Plug-ins 菜单里已经有别的第三方插件，那个目录就是对的。常见：
- `%USERPROFILE%\abaqus_plugins`
- 某个固定的项目目录，例如 `E:\Abaqus2023\Temp\abaqus_plugins`（若 CAE 从 `E:\Abaqus2023\Temp` 启动）

**B. 查环境文件**
搜 `abaqus_v6.env` 里的 `plugin_central_dir`：

```bat
findstr /S /I "plugin_central_dir" abaqus_v6.env
```

**C. 看 CAE 从哪儿启动的**
在 Abaqus/CAE 的 `File -^> Set Work Directory` 看当前目录，该目录下的 `abaqus_plugins` 就会被扫描。

## 定好之后怎么装

设环境变量再跑脚本，脚本就会装到你指定处：

```bat
set ABAQUS_MCP_PLUGIN_DIR=你的\abaqus_plugins
install_plugin.bat
```

不设的话，脚本默认用 `E:\Abaqus2023\Temp\abaqus_plugins`（作者本机路径），
**换机器后大概率不对**，会直接报目录不存在。所以一律建议显式指定。

## 装完怎么确认

1. **完全重启 CAE**（插件只在启动时加载，热改无效）。
2. 菜单里应出现 `Plug-ins -^> Abaqus-Control-MCP`。
3. 点 `Start MCP Bridge`，消息区打印 `Listening on 127.0.0.1:48152`。
4. 回到普通命令行验证：

```bat
python -m abaqus_mcp_bridge.cli check
```

期望看到 `ok:true` 且 `python: 2.7.15`。若报 `WinError 10061`，说明 CAE 里的桥
还没启动（第 3 步没做），不是安装失败。

## 为什么必须是嵌套文件夹结构

Abaqus 2023 只加载这种布局：

```
abaqus_plugins\
└── AbaqusMcpBridge_1.0.0\
    ├── AbaqusMcpBridge_plugin.py     ← 入口，必须有 _plugin.py 后缀
    └── AbaqusMcpBridge\              ← 同名的子包（代码本体）
        ├── __init__.py
        ├── diagnostics.py
        └── gui_form.py
```

直接把 `AbaqusMcpBridge_plugin.py` 扁平丢进 `abaqus_plugins` 是**不会生效**的——
它会被编译但不会执行，菜单里永远看不到入口。`install_plugin.bat` 已按此结构
自动构建并拷贝，无需手工调整。
