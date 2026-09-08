"""Build the deployable Abaqus CAE plugin package from this source tree.

Abaqus 2023 only loads GUI plug-ins that live in a <Name>_<ver>/<Name>_plugin.py
+ <Name>/ sub-package layout (a flat .py in the plugins dir is compiled but never
run). This script produces exactly that folder from the single-source files in
abaqus_mcp_bridge/, so you can re-deploy after editing the real source:

    python build_plugin_pkg.py                 # -> _pkg_out/AbaqusMcpBridge_1.0.0

Then copy that folder into every CAE plug-in directory Abaqus scans, e.g.:
    copy /Y _pkg_out\\AbaqusMcpBridge_1.0.0  E:\\Abaqus2023\\Temp\\abaqus_plugins\\
Run from a fresh Command Prompt and restart CAE afterwards.
"""
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "abaqus_mcp_bridge")
OUT = os.path.join(HERE, "_pkg_out", "AbaqusMcpBridge_1.0.0")
PKG = os.path.join(OUT, "AbaqusMcpBridge")

MARKER = 'try:\n    _log("top-level reached; registering menu buttons")'

gui_src = open(os.path.join(SRC, "gui_plugin.py"), encoding="utf-8").read()
idx = gui_src.find(MARKER)
if idx == -1:
    raise SystemExit("gui_plugin.py registration marker not found; aborting")
gui_body = gui_src[:idx]  # everything before the registration tail

if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(PKG)

# diagnostics + package init are copied verbatim.
shutil.copy2(os.path.join(SRC, "diagnostics.py"), os.path.join(PKG, "diagnostics.py"))
open(os.path.join(PKG, "__init__.py"), "w", encoding="utf-8").write(
    "# -*- coding: utf-8 -*-\n__version__='1.0.0'\n"
)
# gui_plugin.py minus its registration tail becomes the sub-package module.
open(os.path.join(PKG, "gui_form.py"), "w", encoding="utf-8").write(gui_body)

# Thin registration entry point at the package root.
entry = (
    "# -*- coding: utf-8 -*-\n"
    "from abaqusGui import getAFXApp\n"
    "from AbaqusMcpBridge.gui_form import McpGuiActionForm\n\n"
    "toolset = getAFXApp().getAFXMainWindow().getPluginToolset()\n"
    "for action in ('start', 'stop'):\n"
    "    label = 'Start MCP Bridge' if action == 'start' else 'Stop MCP Bridge'\n"
    "    desc = ('Start the TCP bridge for the active Abaqus/CAE session.' if action == 'start'\n"
    "            else 'Stop the TCP bridge.')\n"
    "    toolset.registerGuiMenuButton(\n"
    "        object=McpGuiActionForm(toolset, action),\n"
    "        buttonText='Abaqus-Control-MCP|' + label,\n"
    "        version='1.0.0',\n"
    "        applicableModules=['Part', 'Property', 'Assembly', 'Step', 'Interaction',\n"
    "                           'Load', 'Mesh', 'Job', 'Visualization'],\n"
    "        description=desc,\n"
    "    )\n"
)
open(os.path.join(OUT, "AbaqusMcpBridge_plugin.py"), "w", encoding="utf-8").write(entry)

print("built:", OUT)
for root, _, files in os.walk(OUT):
    for f in sorted(files):
        print("  ", os.path.join(root, f))
