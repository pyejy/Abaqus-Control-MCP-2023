"""MCP stdio server that forwards Python execution requests to Abaqus.

Core tools: ping, run_python — plus viewport capture and ODB metadata.
Most debugging should come from run_python's structured error payload.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

from .client import AbaqusBridgeClient


def _ensure_gui_plugin() -> None:
    """Install the GUI plugin silently if not already present.

    NOTE (2026-09-08, Abaqus-2023 port): this legacy auto-install wrote a flat
    ``abaqus_mcp_gui_plugin.py`` into ``~/abaqus_plugins`` by default. Abaqus
    2023 loads plugins from BOTH that home dir and the site dir
    (``E:\\Abaqus2023\\Temp\\abaqus_plugins``), so the flat copy registered the
    same "Abaqus-Control-MCP" menu as the proper sub-package
    (AbaqusMcpBridge_1.0.0, installed by install_plugin.bat) -- the duplicate
    then hijacked Start/port 48152 with a stale template. Disabled by default;
    only write the flat file when ABAQUS_MCP_PLUGIN_DIR is explicitly set.
    Prefer install_plugin.bat (sub-package layout) instead.
    """
    plugin_dir = os.environ.get("ABAQUS_MCP_PLUGIN_DIR")
    if not plugin_dir:
        return  # home flat-copy auto-install disabled (see note above)
    try:
        import shutil
        from importlib import resources
        from pathlib import Path

        target_dir = Path(plugin_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "abaqus_mcp_gui_plugin.py"

        source = resources.files("abaqus_mcp_bridge").joinpath("gui_plugin.py")
        with resources.as_file(source) as src:
            if target.exists() and target.read_bytes() == Path(src).read_bytes():
                return
            shutil.copy2(src, target)
    except Exception:
        pass


_ensure_gui_plugin()


DEFAULT_HOST = os.environ.get("ABAQUS_MCP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("ABAQUS_MCP_PORT", "48152"))
DEFAULT_TIMEOUT = float(os.environ.get("ABAQUS_MCP_TIMEOUT", "60"))

INSTRUCTIONS = """You are an elite simulation engineer controlling a live Abaqus/CAE session via MCP tools.
Your goal is to build robust, highly stable, and production-grade finite element models.

CORE SIMULATION RULES:
1. SEMANTIC GEOMETRY (Golden Rule): Better not use raw coordinates or `findAt()` to assign boundary conditions, sections, or loads.
    - Immediately after creating a part or feature, grab its geometry using robust methods:
      * `getByBoundingBox(xMin, yMin, zMin, xMax, yMax, zMax)`
      * `getByBoundingCylinder(...)`
      * Topological filtering (e.g., `part.faces[0:1]`, `part.edges.findAt(...)` only for static boundaries).
    - Wrap the grabbed geometry into named Sets (for cells/nodes/vertices) or Surfaces (for faces/edges) IMMEDIATELY.
    - All subsequent steps (meshing, section assignments, interactions, loads, and BCs) MUST reference these semantic names (e.g., `region=part.sets['Set-Name']` or `surface=instance.surfaces['Surf-Name']`).

2. DYNAMIC CHUNKING: Do not write massive scripts that perform modeling, meshing, solver submission, and post-processing all at once.
    - Divide your work into logical validation milestones (e.g., Phase 1: Base Geometry & Named Sets -> Phase 2: Assembly, Step & Mesh -> Phase 3: Physics, Loads & Submission).
    - Verify each phase's correctness via execution before proceeding. No rigid linear chains — adapt the phase size to the task's complexity.

3. DOCKING & DIAGNOSTICS (No Guessing & Web Search): Never guess Abaqus API methods, attributes, or signatures.
    - If live local reflection is insufficient, or if you encounter complex API signature mismatches, ALWAYS call the web search tool (including the term 'Abaqus' and target versions, e.g., 'Abaqus 2024 Python API ...') to find official documentation, forum examples, or method signatures.


4. WORKING DIRECTORY & PERSISTENCE: Every Abaqus analysis generates a massive number of solver files (.inp, .odb, .sta, .msg, etc.).
    - Before writing any model, ALWAYS query or set a clean working directory (using the `set_workdir` tool or standard Python `os.chdir` at the start of your script). Do NOT let Abaqus run in default system paths, which causes permission errors and directory pollution.
    - Periodically save the database (.cae file) to the working directory using `mdb.saveAs(pathName=...)` (e.g., `mdb.saveAs(pathName='D:/temp/My-Model.cae')`). Never leave the CAE session unsaved, ensuring persistence against solver crashes.

5. CONCISE PAIR-PROGRAMMING: Avoid robotic headers, repetitive intent declarations, and verbose apologies. Keep explanations technical, clear, and direct. Focus on finite element modeling best practices.

PREFERRED TOOLS OVER RAW run_python (fall back to run_python only for these can't express):
- get_model_info(model_name?, detail) — inspect current model state in ONE call. Use it FIRST to see parts/sets/steps/loads/jobs before editing, instead of guessing names. detail='full' adds per-entity types when you need them.
- submit_job(job_name, model_name?, cpus?, blocking=False) — create+submit a job. Leave blocking=False and poll with monitor_job_status; do NOT put waitForCompletion() inside run_python (it can freeze the CAE kernel thread).
- extract_kpis(odb_path, queries=[...]) — pull result NUMBERS (max/min/mean Mises at a set, tip displacement, reaction sum…) as scalars. This is how you read results; NEVER loop field.values yourself and set them as `result`.
- read_odb_field / read_odb_history — paged or CSV-exported extraction when you truly need arrays (offset/limit, export_csv). History is auto-downsampled.
RESULT-SIZE RULE: run_python truncates an oversized `result` automatically (returns {__truncated__, total, sample}). So return SCALARS or small dicts from run_python; for big data use export_csv=… + read the file, or write CSV/NPY to the workdir and return its path. Do not try to serialize thousands of nodes/elements inline.

"""

mcp = FastMCP("abaqus-control-mcp", instructions=INSTRUCTIONS)


def _client(timeout: float | None = None) -> AbaqusBridgeClient:
    return AbaqusBridgeClient(
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# Generic python execution wrapper
# ---------------------------------------------------------------------------


async def _exec(code: str, timeout: float | None = None) -> dict[str, Any]:
    """Execute Python code in Abaqus and return the result dict."""
    try:
        return await anyio.to_thread.run_sync(_client(timeout).execute, code)
    except ConnectionRefusedError as e:
        raise RuntimeError(
            "无法连接到 Abaqus 桥接服务 (Connection Refused)。\n"
            "请检查：\n"
            "1. 是否已经正常启动了 Abaqus/CAE 软件？\n"
            "2. 是否已在 Abaqus 顶部菜单栏激活了插件：Plug-ins -> Abaqus-Control-MCP -> Start MCP Bridge\n"
            f"原报错信息: {e}"
        )
    except TimeoutError as e:
        raise RuntimeError(
            "连接 Abaqus 桥接服务超时 (Timeout)。\n"
            "请检查 Abaqus 是否已无响应，或尝试在 Abaqus 中重启插件 (Stop & Start MCP Bridge)。\n"
            f"原报错信息: {e}"
        )


def _format_error_to_markdown(result: dict[str, Any]) -> str:
    """Render a crash payload into a compact, highly readable Markdown diagnostic panel."""
    parts: list[str] = []

    error_type = result.get("error_type", "Unknown")
    short_type = error_type.rsplit(".", 1)[-1] if "." in error_type else error_type
    core_error = result.get("core_error", "Unknown error")
    error_line = result.get("error_line")

    # Extract message from core_error to avoid prefix duplication
    prefix = f"{short_type}:"
    if core_error.startswith(prefix):
        msg = core_error[len(prefix):].strip()
    else:
        msg = core_error

    location = f" at line {error_line}" if error_line else ""
    parts.append(f"{short_type}{location}: {msg}")

    # Indented recovery details
    recovery = result.get("recovery") or {}
    if recovery:
        # KeyError details
        if "missing_key" in recovery:
            if recovery.get("parent_object_path"):
                parts.append(f"  Container: {recovery['parent_object_path']}")
            if "available_keys_sample" in recovery:
                sample = recovery["available_keys_sample"]
                if isinstance(sample, list):
                    if len(sample) > 20:
                        sample = sample[:20] + ["..."]
                    parts.append(f"  Available: {sample}")
            if recovery.get("possible_keys"):
                parts.append(f"  Similar: {recovery['possible_keys']}")

        # AttributeError details
        elif "missing_attribute" in recovery:
            parts.append(f"  Missing Attribute: {recovery['missing_attribute']}")
            if recovery.get("object_type"):
                parts.append(f"  Object Type: {recovery['object_type']}")
            if recovery.get("parent_object_path"):
                parts.append(f"  Object Path: {recovery['parent_object_path']}")
            if recovery.get("possible_members"):
                parts.append(f"  Similar: {recovery['possible_members']}")

        # NameError details
        elif "missing_variable" in recovery:
            parts.append(f"  Undefined Variable: {recovery['missing_variable']}")
            if recovery.get("import_suggestion"):
                parts.append(f"  Import Suggestion: {recovery['import_suggestion']}")

        # SyntaxError details
        elif "syntax_line" in recovery:
            if recovery.get("syntax_offset"):
                parts.append(f"  Syntax Error offset: {recovery['syntax_offset']}")
            if recovery.get("syntax_text"):
                parts.append(f"  Problem text: {recovery['syntax_text'].strip()}")

        # TypeError or fallback callable details
        elif "callable_signature" in recovery or "call_target" in recovery:
            if recovery.get("call_target"):
                parts.append(f"  Call Target: {recovery['call_target']}")
            if recovery.get("callable_signature"):
                parts.append(f"  Expected Signature: {recovery['callable_signature']}")
            if recovery.get("callable_summary"):
                parts.append(f"  Description: {recovery['callable_summary']}")
            if recovery.get("possible_keywords"):
                parts.append(f"  Similar Keywords: {recovery['possible_keywords']}")

    # Failed code line
    code_excerpt = result.get("code_excerpt")
    if code_excerpt:
        failed_line = None
        for line in code_excerpt.splitlines():
            if line.startswith(">>"):
                parts_line = line.split("|", 1)
                if len(parts_line) == 2:
                    failed_line = parts_line[1].strip()
                break
        if failed_line:
            parts.append(f"  Code: {failed_line}")

    # stdout/stderr summary & content
    stdout = result.get("stdout", "").strip()
    stderr = result.get("stderr", "").strip()
    if stdout:
        lines_count = len(stdout.splitlines())
        if lines_count <= 3:
            parts.append(f"  stdout: {stdout}")
        else:
            parts.append(f"  stdout: (captured, {lines_count} lines)")
    if stderr:
        lines_count = len(stderr.splitlines())
        if lines_count <= 3:
            parts.append(f"  stderr: {stderr}")
        else:
            parts.append(f"  stderr: (captured, {lines_count} lines)")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Core tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def ping(timeout: float | None = None) -> dict[str, Any]:
    """Check whether the Abaqus-side bridge agent is reachable.

    Returns session state including Python version, platform, PID, CPU count,
    models, and viewports.
    """
    return await _exec(
        "from abaqus import mdb, session\n"
        "import os, sys, platform\n"
        "try:\n"
        "    _cpus = os.cpu_count()\n"
        "except Exception:\n"
        "    _cpus = None\n"
        "result = {\n"
        "  'python': sys.version,\n"
        "  'executable': sys.executable,\n"
        "  'platform': platform.platform(),\n"
        "  'pid': os.getpid(),\n"
        "  'cpu_count': _cpus,\n"
        "  'abaqus_version': getattr(session, 'version', None),\n"
        "  'models': list(mdb.models.keys()),\n"
        "  'viewports': list(session.viewports.keys()),\n"
        "}",
        timeout,
    )


@mcp.tool()
async def run_python(code: str, timeout: float | None = None) -> dict[str, Any]:
    """Execute Python code in the active Abaqus/CAE kernel.

    Single-line expressions are evaluated and their value returned.
    Multi-line code is executed; set a variable named ``result`` to return data.
    Stdout, stderr, traceback, error line, and code excerpts are included in the response.
    """
    if not code.strip():
        raise ValueError("code must not be empty")
    result = await _exec(code, timeout)
    if not result.get("ok", False):
        raise RuntimeError(_format_error_to_markdown(result))
    return result


@mcp.tool()
async def set_workdir(path: str, timeout: float | None = None) -> dict[str, Any]:
    """Change the Abaqus working directory.

    Args:
        path: Absolute path to set as the working directory.
    Returns the previous and new working directory.
    """
    if not path.strip():
        raise ValueError("path must not be empty")
    code = r"""
import os

new_path = __PATH__

result = {}
try:
    old_dir = os.getcwd()
    if not os.path.isdir(new_path):
        result = {'success': False, 'error': 'Directory does not exist: ' + new_path}
    else:
        os.chdir(new_path)
        result = {'success': True, 'previous': old_dir, 'current': os.getcwd()}
except Exception as e:
    import traceback
    result = {'success': False, 'error': str(e), 'traceback': traceback.format_exc()}
""".replace("__PATH__", json.dumps(path.strip()))
    return await _exec(code, timeout)


@mcp.tool()
async def monitor_job_status(job_name: str = "", timeout: float | None = None) -> dict[str, Any]:
    """Monitor Abaqus job status and optional OS-level progress from .sta/.msg files.

    Args:
        job_name: If provided, read [JobName].sta and [JobName].msg in the working
            directory to extract recent progress and diagnostics. If empty, list
            all jobs defined in the current Abaqus session.
    """
    code = r"""
import os
import re
from abaqus import mdb

job_name = __JOB_NAME__

def _tail_lines(path, count):
    try:
        with open(path, 'r') as f:
            lines = f.read().splitlines()
        if count <= 0:
            return []
        return lines[-count:]
    except Exception:
        return []

def _grep_tail(path, patterns, limit):
    try:
        rx = re.compile('|'.join(patterns))
        matches = []
        with open(path, 'r') as f:
            for line in f:
                if rx.search(line):
                    matches.append(line.rstrip())
        return matches[-limit:] if limit > 0 else matches
    except Exception:
        return []

result = {}
if not job_name:
    jobs = []
    for name in mdb.jobs.keys():
        job = mdb.jobs[name]
        j = {'name': name}
        for attr in ('status', 'type', 'model', 'description', 'numCpus', 'numDomains', 'memory', 'explicitPrecision'):
            try:
                val = getattr(job, attr, None)
                if val is not None:
                    j[attr] = str(val)
            except Exception:
                pass
        jobs.append(j)
    result = {'jobs': jobs}
else:
    sta_path = os.path.join(os.getcwd(), job_name + '.sta')
    msg_path = os.path.join(os.getcwd(), job_name + '.msg')
    progress_lines = _tail_lines(sta_path, 5)
    diagnostic_lines = _grep_tail(msg_path, [r'^\*\*\*ERROR', r'^\*\*\*WARNING'], 10)
    result = {
        'job_name': job_name,
        'sta_path': sta_path,
        'msg_path': msg_path,
        'progress_tail': progress_lines,
        'diagnostics_tail': diagnostic_lines,
    }
""".replace("__JOB_NAME__", json.dumps(job_name.strip()))
    return await _exec(code, timeout)


@mcp.tool()
async def get_model_info(
    model_name: str = "",
    detail: str = "summary",
    timeout: float | None = None,
) -> dict[str, Any]:
    """Return a structured snapshot of a model's current state (read-only).

    One call instead of blind run_python reflection. Covers parts, instances,
    materials, sections+assignments, steps, loads/BCs, interactions, named
    sets/surfaces, mesh stats and jobs.

    Args:
        model_name: Model to inspect; empty uses the active model
            (session.currentModel), falling back to the only model if one exists.
        detail: "summary" (default, counts + names, small & safe over MCP) or
            "full" (adds per-entity type/key params — larger, use when needed).
    """
    code = r"""
from abaqus import mdb, session

model_name = __MODEL__
detail = __DETAIL__

def _keys(obj):
    try:
        return list(obj.keys())
    except Exception:
        return []

def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default

def _params(region_obj):
    out = {}
    try:
        for k, v in region_obj.params.items():
            out[k] = str(v)
    except Exception:
        pass
    return out

# ---- resolve target model ------------------------------------------------ #
if not model_name:
    cur = _safe(lambda: session.currentModel)
    if cur and cur in mdb.models:
        model_name = cur
    elif len(mdb.models) == 1:
        model_name = _keys(mdb.models)[0]
result = {}
if not model_name or model_name not in mdb.models:
    result = {
        'success': False,
        'error': 'no model selected' if not model_name else ('model not found: %s' % model_name),
        'available_models': _keys(mdb.models),
    }
else:
    m = mdb.models[model_name]
    full = (detail == 'full')
    info = {'model': model_name}

    r = m.rootAssembly
    parts = _keys(m.parts)
    insts = _keys(r.instances)
    info['parts'] = {
        'count': len(parts),
        'names': parts,
        'types': {p: str(_safe(lambda p=p: m.parts[p].partType, '')) for p in parts} if full else {},
    }
    info['instances'] = {
        'count': len(insts),
        'names': insts,
        'types': {i: str(_safe(lambda i=i: r.instances[i].instanceType, '')) for i in insts} if full else {},
    }

    mats = _keys(m.materials)
    info['materials'] = {
        'count': len(mats),
        'names': mats,
        'has': ({name: [k for k in ('elastic','plastic','density','hyperelastic','thermal','creep','damage_plasticity')
                        if _safe(lambda name=name, k=k: k in m.materials[name]) ]
                 for name in mats} if full else {}),
    }

    secs = _keys(m.sections)
    assign = _safe(lambda: r.sectionAssignments, None)
    n_assign = _safe(lambda: len(assign), 0) if assign is not None else 0
    info['sections'] = {
        'count': len(secs),
        'names': secs,
        'assignments': n_assign,
        'types': ({s: str(_safe(lambda s=s: m.sections[s].sectionType, '')) for s in secs} if full else {}),
    }

    steps = _keys(m.steps)
    info['steps'] = {
        'count': len(steps),
        'names': steps,
        'procedures': ({s: str(_safe(lambda s=s: m.steps[s].procedure, '')) for s in steps} if full else {}),
    }

    loads = _keys(m.loads)
    bcs = _keys(m.boundaryConditions)
    info['loads'] = {
        'count': len(loads),
        'names': loads,
        'by_step': ({l: str(_safe(lambda l=l: m.loads[l].region.category if hasattr(m.loads[l].region,'category') else '', '')) for l in loads} if full else {}),
    }
    info['boundary_conditions'] = {'count': len(bcs), 'names': bcs}

    inter = _keys(m.interactions)
    iprops = _keys(m.interactionProperties)
    constraints = _keys(m.constraints)
    info['interactions'] = {'count': len(inter), 'names': inter,
                            'properties': iprops, 'constraints': constraints}

    a_sets = _keys(r.sets)
    a_surf = _keys(r.surfaces)
    p_sets = {}
    p_surf = {}
    for p in parts:
        ps = _safe(lambda p=p: _keys(m.parts[p].sets), [])
        su = _safe(lambda p=p: _keys(m.parts[p].surfaces), [])
        if ps:
            p_sets[p] = ps
        if su:
            p_surf[p] = su
    info['named_regions'] = {
        'assembly_sets': a_sets, 'assembly_surfaces': a_surf,
        'part_sets': p_sets, 'part_surfaces': p_surf,
    }

    nodes = _safe(lambda: len(r.nodes), 0)
    elems = _safe(lambda: len(r.elements), 0)
    elem_counts = {}
    if full:
        try:
            for el in r.elements:
                et = el.type
                elem_counts[str(et)] = elem_counts.get(str(et), 0) + 1
        except Exception:
            pass
    info['mesh'] = {'nodes': nodes, 'elements': elems,
                    'element_types': elem_counts if full else {}}

    jobs = _keys(mdb.jobs)
    info['jobs'] = {'count': len(jobs), 'names': jobs}

    amps = _keys(m.amplitudes)
    fld = _safe(lambda: _keys(m.fields), [])
    pflds = _safe(lambda: _keys(m.predefinedFields), [])
    info['amplitudes'] = amps
    info['fields'] = fld
    info['predefined_fields'] = pflds

    result = {'success': True, 'data': info}
""".replace("__MODEL__", json.dumps(model_name.strip())) \
   .replace("__DETAIL__", json.dumps(detail.strip().lower()))
    return await _exec(code, timeout)


@mcp.tool()
async def submit_job(
    job_name: str,
    model_name: str = "",
    cpus: int = 0,
    domains: int = 0,
    double_precision: bool = False,
    blocking: bool = False,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Submit a solver job. Non-blocking by default so the CAE GUI thread stays alive.

    Creates the Job if it doesn't exist yet (pass model_name or use the active
    model). Prefer the default ``blocking=False`` + poll with monitor_job_status
    / get_model_info — a blocking waitForCompletion inside run_python can freeze
    the kernel dispatcher for long analyses.

    Args:
        job_name: Name of the job (created if missing).
        model_name: Model the job runs; empty uses session.currentModel.
        cpus: numCpus for parallel/distributed run (0 = solver default / single).
        domains: numDomains (parallel DIRECT); 0 = not set.
        double_precision: Run in double precision.
        blocking: If True, wait for completion (short jobs only) and read the
            final .sta tail. Default False returns immediately after submit.
    """
    code = r"""
import os
from abaqus import mdb, session
import abaqusConstants as C

job_name = __JOB__
model_name = __MODEL__
cpus = __CPUS__
domains = __DOMAINS__
dbl = __DBL__
blocking = __BLOCK__

def _safe(fn, d=None):
    try:
        return fn()
    except Exception:
        return d

if not model_name:
    model_name = _safe(lambda: session.currentModel)
    if not model_name and len(mdb.models) == 1:
        model_name = list(mdb.models.keys())[0]

result = {}
try:
    if job_name in mdb.jobs:
        job = mdb.jobs[job_name]
        created = False
    else:
        if not model_name or model_name not in mdb.models:
            result = {'success': False,
                      'error': 'job does not exist and no valid model to create it from',
                      'available_models': list(mdb.models.keys()),
                      'available_jobs': list(mdb.jobs.keys())}
        else:
            kw = {'name': job_name, 'model': model_name, 'type': C.ANALYSIS}
            job = mdb.Job(**kw)
            created = True

    if result.get('success') is not False:
        # Parallel/precision live on the Job object (Abaqus 2023 Job.submit only
        # accepts consistencyChecking/datacheckJob/continueJob), so setValues first.
        skw = {}
        if cpus and cpus > 0:
            skw['numCpus'] = cpus
        if domains and domains > 0:
            skw['numDomains'] = domains
        if dbl:
            skw['doublePrecision'] = C.ON
        if skw:
            job.setValues(**skw)

        # persist database so the solver sees an on-disk model
        _safe(lambda: mdb.save())

        job.submit()
        status_after = str(_safe(lambda: job.status, ''))

        out = {
            'success': True,
            'job': job_name,
            'created_now': created,
            'model': model_name or str(_safe(lambda: job.model, '')),
            'numCpus': int(_safe(lambda: job.numCpus, 0)),
            'numDomains': int(_safe(lambda: job.numDomains, 0)),
            'status_after_submit': status_after,
            'workdir': os.getcwd(),
            'sta_path': os.path.join(os.getcwd(), job_name + '.sta'),
            'hint': ('poll with monitor_job_status(job_name=%r); .sta/.msg appear as the solve progresses' % job_name),
        }
        if blocking:
            job.waitForCompletion()
            out['completed'] = True
            out['status_final'] = str(_safe(lambda: job.status, ''))
            sta = out['sta_path']
            tail = []
            try:
                with open(sta) as f:
                    tail = f.read().splitlines()[-5:]
            except Exception:
                pass
            out['sta_tail'] = tail
        result = out
except Exception as e:
    import traceback
    result = {'success': False, 'error': str(e), 'traceback': traceback.format_exc()}
""".replace("__JOB__", json.dumps(job_name.strip())) \
   .replace("__MODEL__", json.dumps(model_name.strip())) \
   .replace("__CPUS__", repr(int(cpus))) \
   .replace("__DOMAINS__", repr(int(domains))) \
   .replace("__DBL__", "True" if double_precision else "False") \
   .replace("__BLOCK__", "True" if blocking else "False")
    return await _exec(code, timeout)


@mcp.tool()
async def inspect_odb(odb_path: str, timeout: float | None = None) -> dict[str, Any]:
    """Open an ODB file (read-only) and return its metadata.

    Returns steps (with sliced frames and total time), parts, instances, section points,
    and available field/history output variables with positions and components.
    """
    code = r"""
from abaqus import mdb
from odbAccess import openOdb

odb_path = __ODB_PATH__
info = {}
odb = None
try:
    odb = openOdb(path=odb_path, readOnly=True)
    info['title'] = str(getattr(odb, 'title', ''))
    info['description'] = str(getattr(odb, 'description', ''))
    info['parts'] = list(odb.parts.keys()) if hasattr(odb, 'parts') else []
    info['instances'] = list(odb.rootAssembly.instances.keys()) if hasattr(odb, 'rootAssembly') else []
    steps = []

    def _slice_frames(frames):
        count = len(frames)
        if count <= 5:
            return list(frames)
        idxs = [0]
        for k in range(1, 4):
            idxs.append(int(round(k * (count - 1) / 4.0)))
        idxs.append(count - 1)
        seen = set()
        uniq = []
        for i in idxs:
            if i not in seen and 0 <= i < count:
                uniq.append(i)
                seen.add(i)
        return [frames[i] for i in uniq]

    def _field_meta(field_output):
        meta = {}
        try:
            meta['position'] = str(getattr(field_output, 'position', ''))
        except Exception:
            meta['position'] = ''
        comps = []
        try:
            comps = list(getattr(field_output, 'componentLabels', []) or [])
        except Exception:
            comps = []
        try:
            invariants = list(getattr(field_output, 'validInvariants', []) or [])
        except Exception:
            invariants = []
        meta['components'] = comps + [str(x) for x in invariants if str(x) not in comps]
        return meta

    for sname in odb.steps.keys():
        s = odb.steps[sname]
        frames = []
        for f in _slice_frames(s.frames):
            frames.append({
                'frameId': f.frameId,
                'frameValue': f.frameValue,
                'description': str(getattr(f, 'description', '')),
            })
        step_info = {
            'name': sname,
            'procedure': str(getattr(s, 'procedure', '')),
            'totalTime': getattr(s, 'totalTime', 0.0),
            'frames': frames,
            'description': str(getattr(s, 'description', '')),
        }
        if s.frames:
            try:
                frame = s.frames[0]
                fov = []
                for desc in frame.fieldOutputs.keys():
                    fo = frame.fieldOutputs[desc]
                    meta = _field_meta(fo)
                    meta['name'] = desc
                    fov.append(meta)
                step_info['fieldOutputs'] = fov
            except Exception:
                step_info['fieldOutputs'] = []
            try:
                frame = s.frames[-1]
                hov = []
                for desc in frame.historyOutputs.keys():
                    hov.append(desc)
                step_info['historyOutputs'] = hov
            except Exception:
                step_info['historyOutputs'] = []
        steps.append(step_info)
    info['steps'] = steps
    result = {'success': True, 'data': info}
except Exception as e:
    import traceback
    result = {'success': False, 'error': str(e), 'traceback': traceback.format_exc()}
finally:
    try:
        if odb is not None:
            odb.close()
    except Exception:
        pass
""".replace("__ODB_PATH__", json.dumps(odb_path))
    return await _exec(code, timeout or 60.0)


# ---------------------------------------------------------------------------
# ODB result extraction (paged / server-side reduced — never dumps raw arrays)
# ---------------------------------------------------------------------------

# Shared kernel helper spliced into each ODB tool's snippet. Keeps ONE source of
# truth for region resolution + value reduction so we never ship full field
# arrays over MCP. Python 2.7-safe: no f-strings, stdlib only.
_ODB_HELPERS = r"""
def _pick_step_frames(step):
    return list(step.frames)

def _resolve_region(field_output, root_assembly, part_name, instance, region_type, region_name):
    'Return a subset of field_output limited to a named region, or None (=all).'
    if not region_name:
        return None
    try:
        if region_type == 'el_set' and instance:
            reg = root_assembly.instances[instance].elementSets[region_name]
        elif region_type == 'el_set':
            reg = root_assembly.elementSets[region_name]
        elif region_type == 'node_set' and instance:
            reg = root_assembly.instances[instance].nodeSets[region_name]
        elif region_type == 'surface':
            reg = root_assembly.surfaces[region_name]
        else:
            reg = root_assembly.nodeSets[region_name]
        return field_output.getSubset(region=reg)
    except Exception:
        return None

def _label_of(vv, fallback=0):
    'Entity label for a FieldValue row: nodeLabel for nodal, elementLabel otherwise.'
    lbl = getattr(vv, 'nodeLabel', None)
    if lbl is None:
        lbl = getattr(vv, 'elementLabel', None)
    if lbl is None:
        lbl = getattr(vv, 'label', None)
    return int(lbl) if lbl is not None else fallback

def _value_of(vv, component, invariant):
    'Reduce one Value/FieldValue object to a python scalar/list per selection.'
    try:
        if invariant:
            attr = getattr(vv, invariant.lower(), None)
            if attr is not None:
                return float(attr) if not callable(attr) else float(attr())
        if component is not None:
            data = getattr(vv, 'data', None)
            if data is not None:
                return float(data[int(component)])
            return None
        data = getattr(vv, 'data', None)
        if data is None:
            return None
        n = len(data)
        if n == 1:
            return float(data[0])
        return [float(x) for x in data]
    except Exception:
        return None
"""


@mcp.tool()
async def read_odb_field(
    odb_path: str,
    field: str,
    step: str = "",
    frame_index: int = -1,
    component: int | None = None,
    invariant: str = "",
    region_type: str = "node_set",
    region_name: str = "",
    instance: str = "",
    offset: int = 0,
    limit: int = 100,
    export_csv: str = "",
    timeout: float | None = None,
) -> dict[str, Any]:
    """Read a field output variable from an ODB WITHOUT dumping all values.

    Returns at most ``limit`` rows (offset-paginated) plus the total count, or,
    if ``export_csv`` is set, writes every row to that CSV path and returns
    {path, rows, columns, preview_head} instead of inline data. This is the
    token-safe way to pull displacement/stress/etc.

    Args:
        odb_path: Path to the .odb file.
        field: FieldOutput name, e.g. 'U', 'S', 'PEEQ', 'RF', 'NT'.
        step: Step name; empty uses the last step.
        frame_index: Frame within the step (-1 = last, i.e. final increment).
        component: For tensor/vector fields pick one component index (0-based);
            leave None to get magnitude/all components (see ``invariant`` first).
        invariant: Preferred reduction for tensors/vectors: 'Mises','MaxPrincipal',
            'Intensity','Magnitude','Phase' etc. Takes precedence over component.
        region_type: node_set | el_set | surface.
        region_name: Named set/surface to restrict to; empty = whole model.
        instance: Instance the set lives on (for node sets defined per-instance).
        offset/limit: Pagination over the selected values (limit<=500 enforced).
        export_csv: If non-empty, absolute path to write ALL selected rows as CSV
            and return the file handle rather than inline values.
    """
    code = r"""
import os, csv
from odbAccess import openOdb
ODB_PATH=__ODB__; FIELD=__FIELD__; STEP=__STEP__; FI=__FI__
COMPONENT=__COMPONENT__; INVARIANT=__INVARIANT__
RTYPE=__RTYPE__; RNAME=__RNAME__; INST=__INST__
OFFSET=__OFF__; LIMIT=min(__LIMIT__, 500); EXPORT_CSV=__CSV__
__HELPERS__
odb=None
result={}
try:
    odb=openOdb(path=ODB_PATH, readOnly=True)
    ra=odb.rootAssembly
    steps=list(odb.steps.keys())
    if not steps:
        result={'success':False,'error':'odb has no steps'}
    else:
        sname = STEP if STEP in odb.steps else steps[-1]
        step_obj=odb.steps[sname]
        frames=_pick_step_frames(step_obj)
        fi = FI if (FI>=0 and FI<len(frames)) else len(frames)-1
        frame=frames[fi]
        if FIELD not in frame.fieldOutputs:
            avail=list(frame.fieldOutputs.keys())
            result={'success':False,'error':'field %r not in step %r'%(FIELD,sname),'available_fields':avail}
        else:
            fo=frame.fieldOutputs[FIELD]
            sub=_resolve_region(fo, ra, '', INST, RTYPE, RNAME)
            src = sub if sub is not None else fo
            position=str(getattr(src,'position',''))
            labels=[]; rows=[]
            total=len(src.values)
            comp_labels=list(getattr(fo,'componentLabels',[]) or [])
            inv_labels=[str(x) for x in (getattr(fo,'validInvariants',[]) or [])]
            idx_end=min(total, OFFSET+LIMIT)
            vals=src.values
            for i in range(OFFSET, idx_end):
                vv=vals[i]
                labels.append(_label_of(vv, i))
                rows.append(_value_of(vv, COMPONENT, INVARIANT if INVARIANT else None))
            out={
                'success':True,
                'odb':os.path.basename(ODB_PATH),
                'step':sname,'frame_index':fi,'frame_value':float(frame.frameValue),
                'field':FIELD,'position':position,
                'total_values':total,'returned':len(rows),
                'offset':OFFSET,'next_offset':(idx_end if idx_end<total else None),
                'components':comp_labels,'invariants':inv_labels,
                'reduction':('invariant:'+INVARIANT) if INVARIANT else (('component:%s'%COMPONENT) if COMPONENT is not None else 'raw'),
            }
            if EXPORT_CSV:
                p=EXPORT_CSV
                try:
                    d=os.path.dirname(p)
                    if d and not os.path.isdir(d): os.makedirs(d)
                    with open(p,'wb') as fh:
                        w=csv.writer(fh)
                        w.writerow(['label','value'])
                        # re-scan ALL values for the file (not just the page)
                        allv=src.values
                        for k in range(len(allv)):
                            w.writerow([_label_of(allv[k], k),
                                        _value_of(allv[k], COMPONENT, INVARIANT if INVARIANT else None)])
                    out['csv']={'path':p,'rows_written':total,'columns':['label','value']}
                    out['preview_head']=[[labels[j],rows[j]] for j in range(min(10,len(rows)))]
                except Exception as ce:
                    out['csv_error']=str(ce)
            else:
                out['data']=[[labels[j],rows[j]] for j in range(len(labels))]
            result=out
except Exception as e:
    import traceback
    result={'success':False,'error':str(e),'traceback':traceback.format_exc()}
finally:
    try:
        if odb is not None: odb.close()
    except Exception: pass
""".replace("__ODB__", json.dumps(odb_path)) \
   .replace("__FIELD__", json.dumps(field)) \
   .replace("__STEP__", json.dumps(step)) \
   .replace("__FI__", repr(int(frame_index))) \
   .replace("__COMPONENT__", repr(component) if component is not None else "None") \
   .replace("__INVARIANT__", json.dumps(invariant.strip())) \
   .replace("__RTYPE__", json.dumps(region_type.strip().lower())) \
   .replace("__RNAME__", json.dumps(region_name.strip())) \
   .replace("__INST__", json.dumps(instance.strip())) \
   .replace("__OFF__", repr(max(0, int(offset)))) \
   .replace("__LIMIT__", repr(max(1, int(limit)))) \
   .replace("__CSV__", json.dumps(export_csv.strip())) \
   .replace("__HELPERS__", _ODB_HELPERS)
    return await _exec(code, timeout or 90.0)


@mcp.tool()
async def read_odb_history(
    odb_path: str,
    series: str,
    step: str = "",
    instance: str = "",
    region_name: str = "",
    limit_points: int = 200,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Extract one history output time series (e.g. a probe point's U2 vs time).

    History arrays are downsampled to at most ``limit_points`` evenly spaced
    samples plus the true total, so long transient runs stay compact.

    Args:
        odb_path: Path to the .odb.
        series: HistoryOutput key, e.g. 'NL:EVERYTHING/U2: ASSEMBLY_NSET.P-POINT1'.
            Pass a substring; exact-key match preferred, else first containing key.
        step: Restrict to a step name; empty scans the last step.
        instance/region_name: Reserved for future region filtering (currently the
            full series is returned since history keys already encode the probe).
        limit_points: Max samples returned (evenly spaced across the real series).
    """
    code = r"""
import os
from odbAccess import openOdb
ODB_PATH=__ODB__; SERIES=__SERIES__; STEP=__STEP__; LP=max(2,min(__LP__,2000))
odb=None; result={}
try:
    odb=openOdb(path=ODB_PATH, readOnly=True)
    steps=list(odb.steps.keys())
    snames=[STEP] if STEP in odb.steps else steps
    found=None; fname=''; tot=0
    for sn in snames:
        ho=odb.steps[sn].historyRegions
        for hrn in ho.keys():
            hocs=ho[hrn].historyOutputs
            keys=list(hocs.keys())
            cand=[k for k in keys if k==SERIES] or [k for k in keys if SERIES in k]
            if cand:
                ho_=hocs[cand[0]]; xy=ho_.data
                tot=len(xy)
                if tot<=LP:
                    pts=[[float(t),float(v)] for (t,v) in xy]
                else:
                    stride=float(tot-1)/(LP-1)
                    idxs=sorted(set(int(round(i*stride)) for i in range(LP)))
                    pts=[[float(xy[j][0]),float(xy[j][1])] for j in idxs]
                found={'step':sn,'series_key':cand[0],'total_points':tot,
                       'returned_points':len(pts),'data':pts}
                break
        if found: break
    if found is None:
        # list available keys to help the caller
        avail=[]
        try:
            sn=snames[-1]
            for hrn in odb.steps[sn].historyRegions.keys():
                avail.extend(list(odb.steps[sn].historyRegions[hrn].historyOutputs.keys()))
        except Exception: pass
        result={'success':False,'error':'history series %r not found'%SERIES,
                'available_keys_sample':avail[:50],'available_count':len(avail)}
    else:
        result={'success':True,'odb':os.path.basename(ODB_PATH),
                'downsampled':found['total_points']>found['returned_points']}
        result.update(found)
except Exception as e:
    import traceback
    result={'success':False,'error':str(e),'traceback':traceback.format_exc()}
finally:
    try:
        if odb is not None: odb.close()
    except Exception: pass
""".replace("__ODB__", json.dumps(odb_path)) \
   .replace("__SERIES__", json.dumps(series)) \
   .replace("__STEP__", json.dumps(step)) \
   .replace("__LP__", repr(int(limit_points)))
    return await _exec(code, timeout or 90.0)


@mcp.tool()
async def extract_kpis(
    odb_path: str,
    queries: list[dict[str, Any]],
    timeout: float | None = None,
) -> dict[str, Any]:
    """Reduce many ODB quantities to SCALARS in one call — the LLM sees numbers, not arrays.

    Each query is a small recipe dict::

        {"label": "maxMises_tip",      # your name for the result
         "source": "field",            # "field" or "history"
         "field": "S",                 # field var (source=field)
         "invariant": "Mises",         # Mises|MaxPrincipal|Intensity|Magnitude|Phase
         "component": null,            # optional single-component index instead of invariant
         "step": "Step-Load",          # empty = last step
         "frame_index": -1,            # empty/-1 = last frame
         "region_type": "node_set",    # node_set|el_set|surface
         "region_name": "NSET-TIP",    # empty = whole model
         "instance": "",               # per-instance set owner (optional)
         "reducer": "max"}             # max|min|mean|sum|first|last|count

    For source="history": use {"source":"history","series":"...U2: ...","reducer":"max"}
    (reduces over the sampled series values). Returns {label: value} map + per-query
    errors. Nothing but scalars crosses the wire.
    """
    import json as _json
    code = r"""
import os, math
from odbAccess import openOdb
ODB_PATH=__ODB__; QUERIES=__QUERIES__
__HELPERS__
def _reduce(seq, reducer):
    xs=[x for x in seq if isinstance(x,(int,float))]
    if not xs: return None
    r=reducer or 'max'
    if r=='max': return max(xs)
    if r=='min': return min(xs)
    if r=='mean': return sum(xs)/float(len(xs))
    if r=='sum': return sum(xs)
    if r=='first': return xs[0]
    if r=='last': return xs[-1]
    if r=='count': return len(xs)
    return max(xs)

odb=None; kpis={}; errs={}
try:
    odb=openOdb(path=ODB_PATH, readOnly=True)
    ra=odb.steps
    for qi,q in enumerate(QUERIES):
        label=q.get('label') or ('q%d'%qi)
        try:
            src=(q.get('source') or 'field').lower()
            steps=list(odb.steps.keys())
            if not steps: raise ValueError('no steps')
            sname=q.get('step') or ''
            if sname not in odb.steps: sname=steps[-1]
            step_obj=odb.steps[sname]
            if src=='history':
                want=q.get('series',''); found=None
                for sn in ([sname] if q.get('step') in odb.steps else steps):
                    ho=odb.steps[sn].historyRegions
                    hit=False
                    for hrn in ho.keys():
                        hocs=ho[hrn].historyOutputs; keys=list(hocs.keys())
                        cand=[k for k in keys if k==want] or [k for k in keys if want in k]
                        if cand:
                            data=hocs[cand[0]].data
                            found=(sn,[float(v) for (_,v) in data]); hit=True; break
                    if hit: break
                if not found: raise ValueError('history series %r not found'%want)
                val=_reduce(found[1], q.get('reducer'))
                kpis[label]=val
            else:
                frames=_pick_step_frames(step_obj)
                fi=q.get('frame_index', -1)
                fi = fi if (isinstance(fi,int) and 0<=fi<len(frames)) else len(frames)-1
                frame=frames[fi]
                fld=q.get('field')
                if not fld or fld not in frame.fieldOutputs:
                    raise ValueError('field %r missing in %s'%(fld,sname))
                fo=frame.fieldOutputs[fld]
                sub=_resolve_region(fo, odb.rootAssembly, '', q.get('instance',''),
                                    q.get('region_type','node_set'), q.get('region_name',''))
                s2 = sub if sub is not None else fo
                comp=q.get('component'); inv=q.get('invariant','')
                vv=[_value_of(x, comp, inv if inv else None) for x in s2.values]
                flat=[]
                for x in vv:
                    if isinstance(x,list): flat.extend(x)
                    elif x is not None: flat.append(x)
                kpis[label]=_reduce(flat, q.get('reducer'))
        except Exception as qe:
            errs[label]=str(qe)
    result={'success':True,'kpis':kpis,'errors':errs,'query_count':len(QUERIES)}
except Exception as e:
    import traceback
    result={'success':False,'error':str(e),'traceback':traceback.format_exc()}
finally:
    try:
        if odb is not None: odb.close()
    except Exception: pass
""".replace("__ODB__", json.dumps(odb_path)) \
   .replace("__QUERIES__", _json.dumps(queries if isinstance(queries, list) else [])) \
   .replace("__HELPERS__", _ODB_HELPERS)
    return await _exec(code, timeout or 120.0)


# ---------------------------------------------------------------------------
# Viewport capture
# ---------------------------------------------------------------------------


@mcp.tool()
async def capture_viewport(
    viewport_name: str = "",
    image_format: str = "PNG",
    timeout: float | None = None,
) -> dict[str, Any]:
    """Capture a screenshot of an Abaqus viewport as a base64-encoded image.

    Leave viewport_name empty to use the current viewport.
    Supported formats: PNG, TIFF, SVG, EPS, PS (falls back to PNG if unsupported).
    """
    code = r"""
import os, tempfile, base64
from abaqus import session
import abaqusConstants

vp_name = __VP__
fmt_str = __FMT__

fmt_map = {
    'PNG': abaqusConstants.PNG,
    'TIFF': abaqusConstants.TIFF,
    'PS': abaqusConstants.PS,
    'EPS': abaqusConstants.EPS,
    'SVG': abaqusConstants.SVG,
}
fmt = fmt_map.get(fmt_str.upper(), abaqusConstants.PNG)

result = {}
try:
    if not vp_name or vp_name not in session.viewports.keys():
        vp_name = session.currentViewportName
    vp = session.viewports[vp_name]
    tmp = tempfile.NamedTemporaryFile(suffix='.' + fmt_str.lower(), delete=False)
    tmp_name = tmp.name
    tmp.close()
    
    session.printToFile(fileName=tmp_name, format=fmt, canvasObjects=(vp,))
    
    with open(tmp_name, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('ascii')
    try:
        os.unlink(tmp_name)
    except Exception:
        pass
    result = {
        'success': True,
        'viewport': vp_name,
        'format': fmt_str.lower(),
        'image_base64': b64,
        'size_bytes': len(b64) * 3 // 4,
    }
except Exception as e:
    import traceback
    result = {'success': False, 'error': str(e), 'traceback': traceback.format_exc()}
""".replace("__VP__", json.dumps(viewport_name)) \
      .replace("__FMT__", json.dumps(image_format.upper()))
    return await _exec(code, timeout or 60.0)


# ---------------------------------------------------------------------------
# MCP Resource - real-time status
# ---------------------------------------------------------------------------


@mcp.resource("abaqus://session-telemetry")
def session_telemetry() -> str:
    """Retrieve active Abaqus/CAE session telemetry and environment metadata."""
    import json as _json
    try:
        r = _client(5.0).ping()
        if isinstance(r, dict):
            return _json.dumps(r, indent=2, ensure_ascii=False)
        return _json.dumps({"connected": False, "detail": str(r)}, indent=2)
    except Exception as e:
        return _json.dumps({"connected": False, "error": str(e)}, indent=2)


# ---------------------------------------------------------------------------
# MCP Resource - agent instructions
# ---------------------------------------------------------------------------


@mcp.resource("abaqus://agent-instructions")
def abaqus_agent_instructions() -> str:
    """Mandatory instructions for any AI agent controlling Abaqus via this MCP."""
    return INSTRUCTIONS





def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
