"""Abaqus/CAE GUI-side MCP bridge plugin -- Python 2.7 compatible port.

Dropped into Abaqus' plugin search directory (see cli ``setup`` / the bundled
``install_plugin.bat``). When activated from Plug-ins -> Abaqus-Control-MCP it
starts a TCP server on 127.0.0.1:48152 and, for every request, marshals the
user's code onto the CAE GUI thread via ``sendCommand`` so it executes in the
embedded CPython 2.7.15 kernel with live mdb/session.

Two compatibility layers matter here:

    1. THIS module is itself executed by Abaqus' 2.7 interpreter at plug-in
       load time, so it must be valid 2.7 (no ``queue``, no ``TimeoutError``,
       etc.). We add small shims near the top.

    2. The kernel template (the big string built by ``_kernel_wrapper``) runs
       inside the CAE kernel as a standalone snippet -- it cannot ``import``
       our package. To keep ONE source of truth for the error-diagnostics
       logic, we read ``diagnostics.py`` from this file's own directory and
       splice its whole body into the template. If that read ever fails we
       fall back to a minimal inline executor so the bridge still runs.

The rest of the architecture (dispatcher form, socket handler) mirrors the
upstream project; only version-specific bits changed.
"""

from abaqusGui import (
    AFXForm,
    AFXMode,
    FXMAPFUNC,
    SEL_COMMAND,
    SEL_TIMEOUT,
    getAFXApp,
    sendCommand,
    showAFXErrorDialog,
)

import base64
import json
import os
import platform
try:
    import socketserver              # Python 3 name
except ImportError:
    import SocketServer as socketserver   # Python 2.7 (Abaqus 2023) name
import sys
import tempfile
import threading
import time
import traceback
import uuid


# ---- Early breadcrumb (BEFORE any risky top-level work) -------------------- #
# If Abaqus never shows the menu button, this line tells us whether the module
# was imported at all. Written to a fixed, easy-to-find path.
try:
    _EARLY_LOG = os.path.join(tempfile.gettempdir(), "abaqus_mcp_gui_plugin.log")
    _h = open(_EARLY_LOG, "a")
    _h.write("%s EARLY import begin file=%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), __file__))
    _h.close()
except Exception:
    pass

# ---- Python 2.7 shims ------------------------------------------------------ #
# Abaqus 2023 ships CPython 2.7 where the queue module is capitalised and
# TimeoutError does not exist. Import defensively so this same file also loads
# under a py3 Abaqus (2024+) without edits.
try:
    import Queue as _queue_mod          # py2
except ImportError:
    import queue as _queue_mod          # py3

try:
    TimeoutError = TimeoutError         # py3 builtin
except NameError:
    class TimeoutError(Exception):      # py2 fallback
        pass

try:
    string_types = basestring           # py2
except NameError:
    string_types = str                  # py3


HOST = os.environ.get("ABAQUS_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("ABAQUS_MCP_PORT", "48152"))
LOG_PATH = os.path.join(tempfile.gettempdir(), "abaqus_mcp_gui_plugin.log")


def _log(message):
    try:
        handle = open(LOG_PATH, "a")
        try:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
        finally:
            handle.close()
    except Exception:
        pass


def _announce(message):
    print(message)
    try:
        main_window = getAFXApp().getAFXMainWindow()
        if hasattr(main_window, "writeToMessageArea"):
            main_window.writeToMessageArea(message)
    except Exception:
        pass


def _send(sock, payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sock.sendall(data + b"\n")


def _recv(sock):
    chunks = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("socket closed before a complete message was received")
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunks.append(chunk[:newline])
            break
        chunks.append(chunk)
    return json.loads(b"".join(chunks).decode("utf-8"))


def _load_diagnostics_source():
    """Read diagnostics.py from this plugin's directory (py2/py3 shared logic).

    Returns the full module text, or None if unavailable. The template splices
    it verbatim so the recovery panel behaves identically to the socket agent.
    """
    candidates = []
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "diagnostics.py"))
    # cli setup copies gui_plugin.py next to abaqus_mcp_bridge/diagnostics.py OR
    # flattens both into the plugin dir; cover both layouts.
    candidates.append(os.path.join(here, "abaqus_mcp_bridge", "diagnostics.py"))
    env_dir = os.environ.get("ABAQUS_MCP_BRIDGE_DIR")
    if env_dir:
        candidates.append(os.path.join(env_dir, "diagnostics.py"))
    for path in candidates:
        try:
            handle = open(path, "r")
            try:
                text = handle.read()
            finally:
                handle.close()
            if "def format_execution_error" in text:
                _log("loaded diagnostics from %s" % path)
                return text
        except Exception:
            continue
    _log("diagnostics.py NOT found; using minimal fallback executor")
    return None


_DIAG_SOURCE = _load_diagnostics_source()

# --------------------------------------------------------------------------- #
# sendCommand has an argument-length limit -- shipping the whole 26 KB
# diagnostics module inline blows it ("command arg must be a string or sequence
# of strings"). So we materialise diagnostics.py ONCE to a stable temp path at
# load time, and every sendCommand becomes a small FIXED-size stub that just
# execs that file then runs the user's code. Length now depends only on the
# user snippet, never on the diagnostics payload.
# --------------------------------------------------------------------------- #
_DIAG_FILE = os.path.join(tempfile.gettempdir(), "abaqus_mcp_diagnostics.py")


def _stage_diagnostics():
    """Write diagnostics source to a stable temp file the kernel can load."""
    if _DIAG_SOURCE is None:
        return False
    try:
        handle = open(_DIAG_FILE, "w")
        try:
            handle.write(_DIAG_SOURCE)
        finally:
            handle.close()
        _log("staged diagnostics -> %s" % _DIAG_FILE)
        return True
    except Exception:
        _log("FAILED to stage diagnostics: %s" % traceback.format_exc())
        return False


_HAVE_STAGED_DIAG = _stage_diagnostics()


def _kernel_wrapper(code, response_path):
    encoded_code = base64.b64encode(code.encode("utf-8")).decode("ascii")
    encoded_path = base64.b64encode(response_path.encode("utf-8")).decode("ascii")
    diag_b64 = base64.b64encode(_DIAG_FILE.encode("utf-8")).decode("ascii") if _HAVE_STAGED_DIAG else ""

    header = r'''
import ast
import base64
import contextlib
import difflib
import io
import inspect
import json
import os
import re
import sys
import traceback

code = base64.b64decode("__ABAQUS_MCP_CODE__").decode("utf-8")
response_path = base64.b64decode("__ABAQUS_MCP_RESPONSE__").decode("utf-8")

# Load shared diagnostics from a staged file instead of inlining it, so this
# command stays short enough for Abaqus' sendCommand argument limit.
dg = None
_dg_staged_path = None
try:
    _diag_src = base64.b64decode("__ABAQUS_MCP_DIAG__").decode("utf-8")
    dg = {}
    exec(compile(open(_diag_src, "r").read(), _diag_src, "exec"), dg)
    _dg_staged_path = _diag_src
except Exception:
    dg = None
    _dg_staged_path = None

# The CAE kernel runs under Abaqus' patched py2.7 io where even a fresh
# io.StringIO rejects byte-str writes (TypeError: unicode argument expected).
# If the staged diagnostics failed to load or lacks the tolerant buffer, patch
# sys.stdout/stderr with an equivalent so plain print() never explodes.
class _McpTolerantStdout(object):
    def __init__(self, inner):
        self._inner = inner
    def write(self, s):
        # ASCII byte-str must arrive as unicode: patched streams do a strict
        # type check. Non-ascii byte-str is left as-is (stdlib StringIO at the
        # core accepts it); the TypeError path below flips shapes when needed.
        if isinstance(s, str) and sys.version_info[0] < 3:
            try:
                s = s.decode("ascii")
            except Exception:
                pass
        try:
            return self._inner.write(s)
        except UnicodeEncodeError:
            # ascii-codec core (Abaqus quirk): repr keeps print() alive.
            try:
                return self._inner.write(u"%r" % (s,))
            except Exception:
                try:
                    return self._inner.write(repr(s))
                except Exception:
                    return len(s)
        except TypeError:
            alt = None
            if isinstance(s, bytes):
                try:
                    alt = s.decode("utf-8", "replace")
                except Exception:
                    alt = None
            elif isinstance(s, unicode):  # noqa: F821 (py2 builtin)
                try:
                    alt = s.encode("utf-8", "replace")
                except Exception:
                    alt = None
            if alt is not None:
                try:
                    return self._inner.write(alt)
                except Exception:
                    pass
            try:
                return self._inner.write(u"%r" % (s,))
            except Exception:
                return len(s)
    def writelines(self, lines):
        for line in lines:
            self.write(line)
    @property
    def encoding(self):
        return getattr(self._inner, "encoding", "UTF-8") or "UTF-8"
    def writable(self):
        return True
    def flush(self):
        try:
            self._inner.flush()
        except Exception:
            pass
    def getvalue(self):
        value = self._inner.getvalue()
        if isinstance(value, str):
            try:
                value = value.decode("utf-8", "replace")
            except Exception:
                pass
        return value
    def __getattr__(self, name):
        return getattr(self._inner, name)

def _mcp_patch_stdout():
    if isinstance(sys.stdout, _McpTolerantStdout):
        return
    sys.stdout = _McpTolerantStdout(sys.stdout)
    if isinstance(sys.stderr, _McpTolerantStdout):
        return
    sys.stderr = _McpTolerantStdout(sys.stderr)

try:
    _probe = io.StringIO()
    _probe.write("ascii")
except TypeError:
    _mcp_patch_stdout()
elif dg is not None and callable(dg.get("_mcp_probe_stdout_poisoned")):
    # Same heuristic the fixed executor uses; keeps both layers in lockstep.
    try:
        if dg["_mcp_probe_stdout_poisoned"]():
            _mcp_patch_stdout()
    except Exception:
        pass

# Older staged diagnostics snapshots build capture buffers as raw io.StringIO;
# their _redirect then swaps our tolerant stdout back to a poisoned one. Wrap
# the loaded execute_in_namespace so it always runs on top of the patch.
if dg is not None and callable(dg.get("execute_in_namespace")) \
        and "_TolerantBuf" not in dg \
        and _dg_staged_path is not None:
    _orig_ein = dg["execute_in_namespace"]
    def _ein_patched(code, namespace, lock=None):
        old_out, old_err = sys.stdout, sys.stderr
        _mcp_patch_stdout()
        try:
            return _orig_ein(code, namespace, lock)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
    dg["execute_in_namespace"] = _ein_patched

# Belt-and-braces: if the staged diagnostics predates _make_buf (old snapshots
# capture stdout into a raw io.StringIO), swap in a fresh copy of the file once.
# A broken staging path degrades to the previous behaviour, never worse.
if dg is not None and "_TolerantBuf" not in dg:
    try:
        _staged_b64 = base64.b64encode(_DIAG_FILE.encode("utf-8")).decode("ascii")
        if _staged_b64 != "__ABAQUS_MCP_DIAG__":
            _fresh_path = base64.b64decode(_staged_b64).decode("utf-8")
            _fresh_dg = {}
            exec(compile(open(_fresh_path, "r").read(), _fresh_path, "exec"), _fresh_dg)
            if "_TolerantBuf" in _fresh_dg and callable(_fresh_dg.get("execute_in_namespace")):
                dg = _fresh_dg
    except Exception:
        pass

'''

    if _HAVE_STAGED_DIAG:
        # Full recovery panel via the staged diagnostics module's helpers.
        core = r'''
_mdb_obj = globals().get("mdb")
_session_obj = globals().get("session")
if _mdb_obj is None:
    try:
        from abaqus import mdb as _mdb_obj
    except Exception:
        pass
if _session_obj is None:
    try:
        from abaqus import session as _session_obj
    except Exception:
        pass

namespace = globals().setdefault("_ABAQUS_MCP_GLOBALS", {
    "__name__": "__abaqus_mcp_exec__",
    "__doc__": None,
})
namespace.update({
    "mdb": _mdb_obj,
    "session": _session_obj,
})

if dg is not None and "execute_in_namespace" in dg:
    # Last line of defence against the kernel's patched io: if the staged
    # diagnostics somehow still builds raw io.StringIO capture buffers
    # (pre-_TolerantBuf snapshot), run its executor on top of a tolerant
    # stdout so print() can never raise unicode/str TypeErrors.
    if "_TolerantBuf" not in dg:
        _orig_ein = dg["execute_in_namespace"]
        def _ein_guard(code, namespace, lock=None):
            old_out, old_err = sys.stdout, sys.stderr
            _mcp_patch_stdout()
            try:
                return _orig_ein(code, namespace, lock)
            finally:
                sys.stdout, sys.stderr = old_out, old_err
        result_envelope = _ein_guard(code, namespace)
    else:
        result_envelope = dg["execute_in_namespace"](code, namespace)
else:
    result_envelope = {"ok": False, "core_error": "diagnostics unavailable",
                       "error_type": "RuntimeError", "recovery": {}}
payload = {"ok": True, "result": result_envelope}

with open(response_path, "w") as handle:
    json.dump(payload, handle, ensure_ascii=False)
'''
    else:
        # Minimal fallback: no recovery panel, but run_python still works even
        # if staging diagnostics failed. Kept short by construction.
        core = r'''
_mdb_obj = globals().get("mdb")
_session_obj = globals().get("session")
if _mdb_obj is None:
    try:
        from abaqus import mdb as _mdb_obj
    except Exception:
        pass
if _session_obj is None:
    try:
        from abaqus import session as _session_obj
    except Exception:
        pass

namespace = globals().setdefault("_ABAQUS_MCP_GLOBALS", {
    "__name__": "__abaqus_mcp_exec__",
    "__doc__": None,
})
namespace.update({"mdb": _mdb_obj, "session": _session_obj})

stdout = io.StringIO()
stderr = io.StringIO()
returned = [None]
error_response = [None]

def _run():
    try:
        try:
            parsed = ast.parse(code, mode="eval")
        except SyntaxError:
            parsed = ast.parse(code, mode="exec")
            exec(compile(parsed, "<abaqus-mcp>", "exec"), namespace, namespace)
            returned[0] = namespace.get("result")
        else:
            returned[0] = eval(compile(parsed, "<abaqus-mcp>", "eval"), namespace, namespace)
    except Exception as exc:
        tb_lines = [l for l in traceback.format_exc().strip().splitlines() if l.strip()]
        error_response[0] = {
            "ok": False,
            "core_error": tb_lines[-1] if tb_lines else str(exc),
            "error_type": "%s.%s" % (type(exc).__module__, type(exc).__name__),
            "error_line": getattr(exc, "lineno", None),
            "recovery": {},
        }

old_out, old_err = sys.stdout, sys.stderr
sys.stdout, sys.stderr = stdout, stderr
try:
    _run()
finally:
    sys.stdout, sys.stderr = old_out, old_err

cs, ce = stdout.getvalue(), stderr.getvalue()
if error_response[0] is not None:
    error_response[0]["stdout"] = cs
    error_response[0]["stderr"] = ce
    payload = {"ok": True, "result": error_response[0]}
else:
    payload = {"ok": True, "result": {"ok": True, "return_value": returned[0], "stdout": cs, "stderr": ce}}

with open(response_path, "w") as handle:
    json.dump(payload, handle, ensure_ascii=False)
'''

    template = header + core
    return (
        template.replace("__ABAQUS_MCP_CODE__", encoded_code)
        .replace("__ABAQUS_MCP_RESPONSE__", encoded_path)
        .replace("__ABAQUS_MCP_DIAG__", diag_b64)
    )


def _run_kernel_code(code, timeout):
    response_path = os.path.join(
        tempfile.gettempdir(), "abaqus_mcp_%s.json" % uuid.uuid4().hex
    )
    command = _kernel_wrapper(code, response_path)

    # Abaqus' sendCommand type-checker rejects our long multi-line template
    # (verified empirically: identical short statements run fine). So we stage
    # the full script to a temp file and send only a tiny execfile() call --
    # exactly how standard CAE plug-ins hand code to the kernel. This keeps the
    # sendCommand argument trivially valid regardless of user-code size.
    script_path = os.path.join(
        tempfile.gettempdir(), "abaqus_mcp_cmd_%s.py" % uuid.uuid4().hex
    )
    handle = open(script_path, "w")
    try:
        handle.write(command)
    finally:
        handle.close()
    # Normalize separators for execfile; forward slashes are safe on Windows py2.7.
    script_fwd = script_path.replace("\\", "/")
    resp_fwd = response_path.replace("\\", "/")
    tiny = "execfile('%s')" % script_fwd
    _log("sendCommand start script=%s resp=%s tinylent=%d" % (script_path, response_path, len(tiny)))
    try:
        sendCommand(tiny)
    except Exception as exc:
        _log("sendCommand raised: %s\n%s" % (exc, traceback.format_exc()))
        try:
            os.remove(script_path)
        except Exception:
            pass
        raise

    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(response_path):
            handle = open(response_path, "r")
            try:
                payload = json.load(handle)
            finally:
                handle.close()
            for p in (response_path, script_path):
                try:
                    os.remove(p)
                except Exception:
                    pass
            if not payload.get("ok"):
                error = payload.get("error", {})
                _log("kernel error: %s" % error.get("message", "kernel command failed"))
                raise RuntimeError(error.get("message", "kernel command failed"))
            _log("kernel response ok")
            return payload["result"]
        time.sleep(0.05)

    # No response file: the kernel ran but diagnostics may have failed earlier.
    # Surface whatever partial payload the exec'd script produced, so the caller
    # sees the real 2.7 error instead of a bare timeout.
    if os.path.exists(response_path):
        try:
            handle = open(response_path, "r")
            try:
                payload = json.load(handle)
            finally:
                handle.close()
            try:
                os.remove(response_path)
            except Exception:
                pass
            return payload.get("result", payload)
        except Exception:
            pass

    try:
        os.remove(script_path)
    except Exception:
        pass
    raise TimeoutError("timed out waiting for Abaqus kernel response")


def _sendcommand_selftest():
    """Diagnostic: run at Start time (GUI context) to learn how this Abaqus build
    accepts sendCommand. Writes results to a dedicated probe log so one restart
    pins down the exact accepted signature/shape. Safe to delete after."""
    plog = os.path.join(tempfile.gettempdir(), "abaqus_mcp_sendcommand_probe.log")
    def P(msg):
        try:
            h = open(plog, "a"); h.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg)); h.close()
        except Exception:
            pass
    try:
        import inspect as _insp
        try:
            P("sendCommand sig=%s" % (_insp.signature(sendCommand),))
        except Exception as e:
            P("sig introspection failed: %r" % e)
    except Exception:
        pass
    # marker file path the kernel will write if it actually executes
    mk = os.path.join(tempfile.gettempdir(), "mcp_sc_marker.txt").replace("\\", "/")
    tiny = "open(r'%s','w').write('RAN')" % mk
    full_cmd = _kernel_wrapper("result=6*7", os.path.join(tempfile.gettempdir(), "mcp_probe_resp.json"))
    # Binary-ish search: send growing prefixes; first failing length localises the trigger.
    variants = []
    for frac in (0.10, 0.25, 0.40, 0.55, 0.70, 0.85, 1.00):
        k = int(len(full_cmd) * frac)
        prefix = full_cmd[:k] + "\n"  # newline to keep syntactically harmless truncation
        def make(pfx=prefix, kk=k):
            return lambda: sendCommand(pfx, False, False), kk
        fn, kk = make()
        variants.append(("prefix_%d%%_len%d" % (int(frac*100), kk), fn))
    # Also isolate specific token families in an otherwise-valid wrapper:
    q_only = "a=\"%s\"\nopen(r'%s','w').write('Q')" % ("Z"*800, mk)
    variants.append(("quote_heavy_800", lambda: sendCommand(q_only, False, False)))
    b64_only = "import base64\nb=base64.b64decode('%s')\nopen(r'%s','w').write('B')" % ("aGVsbG8="*100, mk)
    variants.append(("b64token_heavy", lambda: sendCommand(b64_only, False, False)))
    for name, fn in variants:
        try:
            if os.path.exists(mk): os.remove(mk)
        except Exception:
            pass
        raised = None
        try:
            fn()
        except Exception as e:
            raised = repr(e)
        ran = os.path.exists(mk)
        P("[%s] raised=%s kernelRan=%s" % (name, raised, ran))
    P("PROBE DONE -> post this file back: %s" % plog)


class McpGuiHandler(socketserver.BaseRequestHandler):
    def handle(self):
        request_id = None
        try:
            message = _recv(self.request)
            request_id = message.get("id")
            method = message.get("method")
            params = message.get("params") or {}
            _log("request method=%s id=%s" % (method, request_id))

            if _DISPATCHER is None:
                raise RuntimeError("GUI dispatcher is not initialized")

            wait_timeout = float(params.get("timeout") or os.environ.get("ABAQUS_MCP_TIMEOUT", "60")) + 5.0
            item = GuiRequest(method, params)
            _REQUESTS.put(item)
            _log("queued method=%s id=%s" % (method, request_id))

            if not item.event.wait(wait_timeout):
                raise TimeoutError("timed out waiting for GUI dispatcher")
            if item.error is not None:
                raise item.error
            result = item.result

            _send(self.request, {"id": request_id, "ok": True, "result": result})
            _log("response ok id=%s" % request_id)
        except Exception as exc:
            _log("response error id=%s error=%s" % (request_id, exc))
            _send(
                self.request,
                {
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "message": str(exc),
                        "type": "%s.%s" % (type(exc).__module__, type(exc).__name__),
                        "traceback": traceback.format_exc(),
                    },
                },
            )


class McpGuiServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


_SERVER = None
_DISPATCHER = None
_REQUESTS = _queue_mod.Queue()


class GuiRequest:
    def __init__(self, method, params):
        self.method = method
        self.params = params
        self.event = threading.Event()
        self.result = None
        self.error = None


def _handle_on_gui_thread(item):
    method = item.method
    params = item.params
    timeout = float(params.get("timeout") or os.environ.get("ABAQUS_MCP_TIMEOUT", "60"))

    if method == "ping":
        code = (
            "import os, sys, platform\n"
            "from abaqus import mdb, session\n"
            "try:\n"
            "    _cpus = os.cpu_count()\n"
            "except Exception:\n"
            "    _cpus = None\n"
            "result = {'python': sys.version, 'executable': sys.executable, "
            "'platform': platform.platform(), 'pid': os.getpid(), "
            "'cpu_count': _cpus, "
            "'abaqus_version': getattr(session, 'version', None), "
            "'models': list(mdb.models.keys()), "
            "'viewports': list(session.viewports.keys())}"
        )
        result = _run_kernel_code(code, timeout)
        rv = result.get("return_value") if isinstance(result, dict) else None
        if not isinstance(rv, dict):
            # Kernel hit an error instead of returning the dict; expose it fully
            # so the CLI/user sees the real 2.7 traceback rather than a bare key.
            return result
        result = rv
        result["guiProcess"] = {
            "python": sys.version,
            "platform": platform.platform(),
            "thread": threading.current_thread().name,
        }
        return result

    if method == "execute":
        code = params.get("code")
        if not isinstance(code, string_types) or not code.strip():
            raise ValueError("params.code must be a non-empty string")
        return _run_kernel_code(code, timeout)

    raise ValueError("unknown method: %r" % method)


def start_gui_agent():
    global _SERVER
    if _SERVER is not None:
        _log("start requested, already running")
        return "Abaqus MCP GUI agent is already running on %s:%s" % (HOST, PORT)
    _log("starting GUI agent on %s:%s" % (HOST, PORT))
    _SERVER = McpGuiServer((HOST, PORT), McpGuiHandler)
    thread = threading.Thread(target=_SERVER.serve_forever, name="AbaqusMcpGuiAgent")
    thread.daemon = True
    thread.start()
    _log("started GUI agent")
    return "Abaqus MCP GUI agent listening on %s:%s" % (HOST, PORT)


def stop_gui_agent():
    global _SERVER
    if _SERVER is None:
        return "Abaqus MCP GUI agent is not running."
    try:
        _SERVER.shutdown()
        _SERVER.server_close()
    finally:
        _SERVER = None
    _log("stopped GUI agent")
    return "Abaqus MCP GUI agent stopped."


class McpGuiActionForm(AFXForm):
    ID_START = AFXForm.ID_LAST + 1
    ID_STOP = AFXForm.ID_LAST + 2
    ID_POLL = AFXForm.ID_LAST + 3

    def __init__(self, owner, action):
        global _DISPATCHER
        AFXForm.__init__(self, owner)
        self.action = action
        FXMAPFUNC(self, SEL_COMMAND, AFXMode.ID_ACTIVATE, McpGuiActionForm.onCmdActivate)
        FXMAPFUNC(self, SEL_COMMAND, self.ID_START, McpGuiActionForm.onCmdStart)
        FXMAPFUNC(self, SEL_COMMAND, self.ID_STOP, McpGuiActionForm.onCmdStop)
        FXMAPFUNC(self, SEL_TIMEOUT, self.ID_POLL, McpGuiActionForm.onTimeout)
        _DISPATCHER = self

    def onCmdActivate(self, sender, sel, ptr):
        if self.action == "start":
            return self.onCmdStart(sender, sel, ptr)
        elif self.action == "stop":
            return self.onCmdStop(sender, sel, ptr)
        return 1

    def getFirstDialog(self):
        return None

    def _schedule_poll(self):
        getAFXApp().addTimeout(100, self, self.ID_POLL)

    def onTimeout(self, sender, sel, ptr):
        processed = 0
        while processed < 5:
            try:
                item = _REQUESTS.get_nowait()
            except _queue_mod.Empty:
                break
            try:
                _log("GUI thread handling method=%s" % item.method)
                item.result = _handle_on_gui_thread(item)
                _log("GUI thread handled method=%s" % item.method)
            except Exception as exc:
                item.error = exc
                _log("GUI thread error method=%s error=%s\n%s" % (item.method, exc, traceback.format_exc()))
            finally:
                item.event.set()
            processed += 1

        if _SERVER is not None:
            self._schedule_poll()
        return 1

    def onCmdStart(self, sender, sel, ptr):
        try:
            message = start_gui_agent()
            self._schedule_poll()
            if "already running" in message:
                _announce(message)
            else:
                motto = "May your meshes converge and your residuals drop."
                banner = (
                    "\n"
                    "    o---o \n"
                    "   /   /|   < Abaqus MCP Bridge Active!\n"
                    "  o---o o   Listening on %s:%s\n"
                    "  |___|/    <<--  %s  -->>\n"
                ) % (HOST, PORT, motto)
                _announce(banner)
                _announce("Abaqus MCP GUI plugin log: %s" % LOG_PATH)
                if _DIAG_SOURCE is None:
                    _announce("WARNING: diagnostics.py not found next to plugin; "
                              "smart error recovery disabled.")
        except Exception as exc:
            message = "Abaqus MCP GUI agent failed: %s" % exc
            _log(message)
            showAFXErrorDialog(getAFXApp().getAFXMainWindow(), message)
        return 1

    def onCmdStop(self, sender, sel, ptr):
        try:
            message = stop_gui_agent()
            _announce(message)
        except Exception as exc:
            message = "Abaqus MCP GUI agent stop failed: %s" % exc
            _log(message)
            showAFXErrorDialog(getAFXApp().getAFXMainWindow(), message)
        return 1

try:
    _log("top-level reached; registering menu buttons")
    toolset = getAFXApp().getAFXMainWindow().getPluginToolset()
    toolset.registerGuiMenuButton(
        object=McpGuiActionForm(toolset, "start"),
        buttonText="Abaqus-Control-MCP|Start MCP Bridge",
        version="1.0.0",
        applicableModules=["Part", "Property", "Assembly", "Step", "Interaction", "Load", "Mesh", "Job", "Visualization"],
        description="Start the TCP bridge for the active Abaqus/CAE session.",
    )
    toolset.registerGuiMenuButton(
        object=McpGuiActionForm(toolset, "stop"),
        buttonText="Abaqus-Control-MCP|Stop MCP Bridge",
        version="1.0.0",
        applicableModules=["Part", "Property", "Assembly", "Step", "Interaction", "Load", "Mesh", "Job", "Visualization"],
        description="Stop the TCP bridge.",
    )
    _log("menu buttons registered OK")
except Exception as exc:
    try:
        _log("REGISTRATION FAILED: %s" % traceback.format_exc())
    except Exception:
        pass
    raise
