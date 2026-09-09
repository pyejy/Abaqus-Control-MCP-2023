"""Shared, Python 2.7 / 3.x compatible execution-diagnostics helpers.

This module is the "full version" heart of the Abaqus-2023 port. It is written
to a deliberately narrow language subset so the *same file* runs inside:

    * Abaqus/CAE 2023's embedded CPython 2.7.15 kernel (via sendCommand), and
    * any modern CPython 3.x (system side).

Language rules enforced here (do not break them or the 2.7 kernel will refuse
to import this module):

    * No ``from __future__ import annotations`` (harmless on 2.7 but pointless).
    * No type annotations on functions or variables -- none at all.
    * No f-strings; use ``%`` formatting.
    * No PEP 604 unions (``X | Y``) or builtin generics (``dict[str, Any]``).
    * Only stdlib that exists in 2.7: ast, difflib, inspect, io, json, re, sys,
      contextlib, traceback.

The AST layer is the tricky part. CPython changed its AST grammar twice around
the nodes we care about:

    * Literals: 2.7 uses ``ast.Str`` / ``ast.Num`` / ``ast.NameConstant``;
      3.8+ collapses them into ``ast.Constant``. We detect both by attribute
      name (``getattr(ast, "Constant", None)``) so importing never explodes.
    * Subscript slices: 2.7 wraps the slice in ``ast.Index``; 3.9+ removed that
      wrapper so ``node.slice`` is the key expression itself. We unwrap when
      present.
    * ``ast.unparse`` only exists in 3.9+. On 2.7 we recover an expression's
      source text by slicing the original code with the node's own
      lineno/col_offset -- verified byte-for-byte equal to ast.unparse on py3.

Keeping one copy of these functions (imported by BOTH the socket agent and the
GUI-plugin kernel template) is what lets the error panel behave identically no
matter which route executes the user's script.
"""

import ast
import contextlib
import difflib
import inspect
import io
import json
import os
import re
import sys
import traceback


# --------------------------------------------------------------------------- #
# Version-adaptive AST constants. Resolved once, guarded so a missing class
# never raises AttributeError at import time on either interpreter.
# --------------------------------------------------------------------------- #
_AST_CONSTANT = getattr(ast, "Constant", None)          # 3.8+
_AST_STR = getattr(ast, "Str", None)                    # 2.7 (deprecated alias on 3.x)
_AST_NUM = getattr(ast, "Num", None)                    # 2.7
_AST_NAMECONSTANT = getattr(ast, "NameConstant", None)  # 3.4..3.7
_AST_INDEX = getattr(ast, "Index", None)                # <=3.8 subscript wrapper

_LITERAL_TYPES = tuple(
    t for t in (_AST_CONSTANT, _AST_STR, _AST_NUM, _AST_NAMECONSTANT) if t is not None
)


def _is_literal(node):
    return bool(_LITERAL_TYPES) and isinstance(node, _LITERAL_TYPES)


def literal_value(node):
    """Return the python value carried by a literal AST node, or None."""
    if node is None:
        return None
    # On 3.8+ every scalar literal is ast.Constant and stores .value directly.
    if _AST_CONSTANT is not None and isinstance(node, _AST_CONSTANT):
        return getattr(node, "value", None)
    # Legacy nodes keep their payload under .s (str), .n (num), .value (bool/None).
    for attr in ("s", "n", "value"):
        if hasattr(node, attr):
            return getattr(node, attr)
    return None


def unwrap_slice(node):
    """Normalise a Subscript.slice across the Index-wrapper change.

    2.7 / <=3.8: subscript.slice is ast(Index(value=<key expr>))
    3.9+       : subscript.slice is <key expr> directly
    """
    if node is None:
        return None
    if _AST_INDEX is not None and isinstance(node, _AST_INDEX):
        return getattr(node, "value", None)
    return node


def key_literal(node):
    """Literal value of a subscript key, tolerating the Index wrapper."""
    inner = unwrap_slice(node)
    if _is_literal(inner):
        return literal_value(inner)
    return None


# --------------------------------------------------------------------------- #
# Source recovery: faithful replacement for ast.unparse on 2.7.
# --------------------------------------------------------------------------- #
_CURRENT_SOURCE = [None]  # single-element list = mutable module-global cell


def set_current_source(source):
    """Register the code being executed so unparse can slice it later."""
    _CURRENT_SOURCE[0] = source


def _node_end_pos(node):
    end_lineno = getattr(node, "end_lineno", None)
    end_col = getattr(node, "end_col_offset", None)
    if end_lineno is None or end_col is None:
        return None
    return (end_lineno, end_col)


def _dotted_chain_end(line, col):
    """2.7 fallback: end column of a simple dotted-name chain at *col*.

    CPython 2.7 AST nodes carry no end coordinates, so the historical fallback
    sliced a Name/Attribute node from its start column to end-of-line -- that
    dragged in trailing ``[...]`` / ``(...)`` / ``=`` text belonging to the
    ENCLOSING expression (e.g. ``parts`` became ``parts['braket']``), which
    poisoned container-path resolution and hid possible-key suggestions. Every
    internal caller of unparse_node wants the dotted callee/container chain, so
    when end coords are absent we tokenize the physical line and stop the slice
    at the first token that is not part of a dotted attribute chain.
    """
    try:
        import tokenize as _tk
        import io as _io
        # On py2 code may arrive as byte-str; the Abaqus-patched io.StringIO
        # rejects bytes, so normalise to unicode before tokenising.
        if isinstance(line, str):
            try:
                line = line.decode("utf-8", "replace")
            except Exception:
                line = unicode(line, "utf-8", "replace")  # noqa: F821 (py2)
        readline = _io.StringIO(line).readline
        tokens = list(_tk.generate_tokens(readline))
    except Exception:
        return None
    start_i = None
    for i, tok in enumerate(tokens):
        if tok[2][1] == col and tok[1] != "":
            start_i = i
            break
    if start_i is None:
        return None
    i = start_i
    if tokens[i][0] not in (_tk.NAME,):
        return None
    # The tokenizer reports NAME start/end as (row,col) 1-based-style columns but
    # real (row, col) 0-based columns in tok[2]/tok[3]. End col here is the char
    # AFTER the name's last char (col 14 for 'parts' at col 9..14). Verified.
    end_col = tokens[i][3][1]
    i += 1
    # consume `.name` continuations
    while i + 1 < len(tokens):
        if tokens[i][0] == _tk.OP and tokens[i][1] == "." and tokens[i + 1][0] == _tk.NAME:
            end_col = tokens[i + 1][3][1]
            i += 2
        else:
            break
    return end_col


def unparse_node(node, source=None):
    """Best-effort reconstruction of an expression node's source text.

    Prefers the real ``ast.unparse`` (3.9+); otherwise slices the physical
    source using the node coordinates. Returns None if nothing works.
    """
    if node is None:
        return None
    if hasattr(ast, "unparse"):
        try:
            return ast.unparse(node)
        except Exception:
            pass
    if source is None:
        source = _CURRENT_SOURCE[0]
    if not source:
        return None
    lines = source.splitlines()
    start_line = getattr(node, "lineno", None)
    if start_line is None:
        return None
    col = getattr(node, "col_offset", 0) or 0
    idx = start_line - 1
    if idx < 0 or idx >= len(lines):
        return None
    end = _node_end_pos(node)
    if end is None:
        # No end coords (2.7): slice the dotted chain to its true end column
        # instead of running to end-of-line (which would swallow enclosing
        # subscript/call text and break container-path recovery below).
        end_col = _dotted_chain_end(lines[idx], col)
        if end_col is not None:
            seg = lines[idx][col:end_col].strip()
            return seg or None
        seg = lines[idx][col:].strip()
        return seg or None
    end_line, end_col = end
    if end_line == start_line:
        seg = lines[idx][col:end_col]
    else:
        parts = [lines[idx][col:]]
        for i in range(start_line, end_line - 1):
            if 0 <= i < len(lines):
                parts.append(lines[i])
        if 0 <= end_line - 1 < len(lines):
            parts.append(lines[end_line - 1][:end_col])
        seg = "".join(parts)
    seg = seg.strip()
    return seg or None


def extract_tb_lineno(exc):
    """Line number where *exc* was raised, working on 2.7 and 3.x alike.

    2.7 exception objects have no ``__traceback__``; the live traceback must be
    pulled from ``sys.exc_info()[2]``. We try the attribute first (3.x path),
    then fall back to exc_info, then scan for our synthetic filename.
    """
    if getattr(exc, "lineno", None) is not None:
        return getattr(exc, "lineno")
    tb = getattr(exc, "__traceback__", None)
    if tb is None:
        try:
            tb = sys.exc_info()[2]
        except Exception:
            tb = None
    fallback = None
    while tb is not None:
        try:
            fname = tb.tb_frame.f_code.co_filename
        except Exception:
            fname = ""
        if fname == "<abaqus-mcp>":
            return tb.tb_lineno
        if fallback is None:
            fallback = tb.tb_lineno
        tb = tb.tb_next
    return fallback


# --------------------------------------------------------------------------- #
# Expression / object introspection used by the error panel.
# --------------------------------------------------------------------------- #
def resolve_simple_expr(expr, namespace):
    """Evaluate name / attribute / subscript chains without calling anything."""

    def _eval_node(node):
        if isinstance(node, ast.Name):
            return namespace[node.id]
        if isinstance(node, ast.Attribute):
            return getattr(_eval_node(node.value), node.attr)
        if isinstance(node, ast.Subscript):
            base = _eval_node(node.value)
            key_node = unwrap_slice(node.slice)
            try:
                if hasattr(ast, "literal_eval") and _is_literal(key_node):
                    key = literal_value(key_node)
                else:
                    key = ast.literal_eval(node.slice)
            except Exception:
                if isinstance(key_node, ast.Name):
                    key = namespace[key_node.id]
                else:
                    raise
            return base[key]
        raise ValueError("unsupported expression node")

    parsed = ast.parse(expr, mode="eval")
    return _eval_node(parsed.body)


def find_subscript_parent(code, missing_key, lineno=None):
    """Locate the object expression that was indexed by *missing_key*."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and key_literal(node.slice) == missing_key:
            src = unparse_node(node.value, code)
            if src is not None:
                candidates.append((getattr(node, "lineno", 0), src))
    if not candidates and lineno is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and getattr(node, "lineno", None) == lineno:
                src = unparse_node(node.value, code)
                if src is not None:
                    candidates.append((getattr(node, "lineno", None), src))
    if not candidates:
        return None
    if lineno is not None:
        candidates.sort(key=lambda c: abs(c[0] - lineno))
    return candidates[0][1]


def find_attribute_parent(code, missing_attr):
    """Find the object expression whose ``.<missing_attr>`` access failed."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == missing_attr:
            return unparse_node(node.value, code)
    return None


def extract_call_target(code, lineno):
    """Return the callee source of the innermost Call spanning *lineno*."""
    if lineno is None:
        return None
    try:
        tree = ast.parse(code)
    except Exception:
        try:
            lines = code.splitlines()
            if lineno - 1 < 0 or lineno - 1 >= len(lines):
                return None
            tree = ast.parse(lines[lineno - 1], mode="exec")
        except Exception:
            return None

    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", start)
            if start is not None and end is not None:
                if start <= lineno <= end:
                    candidates.append(node)
            elif start == lineno:
                candidates.append(node)
    if not candidates:
        return None
    candidates.sort(
        key=lambda n: getattr(n, "end_lineno", getattr(n, "lineno", 0)) - getattr(n, "lineno", 0)
    )
    return unparse_node(candidates[0].func, code)


def extract_invalid_keyword(msg):
    m1 = re.search(r"got an unexpected keyword argument ['\"](\w+)['\"]", msg)
    if m1:
        return m1.group(1)
    m2 = re.search(r"keyword error on (\w+)", msg)
    if m2:
        return m2.group(1)
    return None


def extract_params_from_sig(sig_str):
    m = re.search(r"\((.*)\)", sig_str)
    if not m:
        return []
    content = m.group(1)
    params = []
    paren_depth = 0
    current = []
    for char in content:
        if char in "([{":
            paren_depth += 1
            current.append(char)
        elif char in ")]}":
            paren_depth -= 1
            current.append(char)
        elif char == "," and paren_depth == 0:
            params.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        params.append("".join(current).strip())
    names = []
    for p in params:
        if not p:
            continue
        word = re.match(r"^([a-zA-Z_]\w*)", p)
        if word:
            name = word.group(1)
            if name not in ("self", "args", "kwargs"):
                names.append(name)
    return names


def extract_signature_from_docstring(doc, target_name):
    pattern = r"\b" + re.escape(target_name) + r"\s*\("
    match = re.search(pattern, doc)
    if not match:
        return None
    start_pos = match.start()
    open_paren_idx = match.end() - 1
    paren_count = 0
    end_pos = -1
    for i in range(open_paren_idx, min(open_paren_idx + 1000, len(doc))):
        char = doc[i]
        if char == "(":
            paren_count += 1
        elif char == ")":
            paren_count -= 1
            if paren_count == 0:
                end_pos = i + 1
                break
    if end_pos != -1:
        return " ".join(doc[start_pos:end_pos].split())
    return None


def summarize_mapping_keys(mapping_obj, missing_key):
    result = {"available_keys_sample": [], "possible_keys": []}
    keys_method = getattr(mapping_obj, "keys", None)
    if not callable(keys_method):
        return result
    try:
        scanned = list(keys_method())
    except Exception:
        return result
    key_texts = [str(k) for k in scanned]
    result["available_keys_sample"] = key_texts
    if missing_key is not None:
        try:
            result["possible_keys"] = difflib.get_close_matches(
                str(missing_key), key_texts, n=8, cutoff=0.45
            )
        except Exception:
            pass
    return result


def summarize_object_members(obj, missing_attr):
    result = {"possible_members": []}
    try:
        members = sorted([name for name in dir(obj) if not name.startswith("_")])
    except Exception:
        return result
    if missing_attr:
        try:
            result["possible_members"] = difflib.get_close_matches(
                missing_attr, members, n=10, cutoff=0.45
            )
        except Exception:
            pass
    return result


def summarize_callable(target_expr, namespace, invalid_keyword=None):
    summary = {
        "call_target": target_expr,
        "callable_signature": None,
        "callable_summary": None,
    }
    if not target_expr:
        return summary
    try:
        target = resolve_simple_expr(target_expr, namespace)
    except Exception:
        return summary

    target_name = getattr(target, "__name__", "function")
    try:
        sig = inspect.signature(target)
        sig_str = "%s%s" % (target_name, sig)
    except Exception:
        sig_str = "%s(...)" % target_name

    doc = None
    try:
        doc = inspect.getdoc(target)
    except Exception:
        pass
    if not doc:
        try:
            doc = getattr(target, "__doc__", None)
        except Exception:
            pass

    if doc:
        try:
            lines = doc.strip().splitlines()
            if lines:
                first_line = lines[0].strip()
                if " -> " in first_line:
                    first_line = first_line.split(" -> ", 1)[1].strip()
                summary["callable_summary"] = first_line if first_line else None
            if sig_str.endswith("(...)"):
                extracted = extract_signature_from_docstring(doc, target_name)
                if extracted:
                    sig_str = extracted
        except Exception:
            pass

    if len(sig_str) > 1000:
        sig_str = sig_str[:997] + "..."
    summary["callable_signature"] = sig_str

    if invalid_keyword:
        try:
            valid_params = []
            try:
                sig = inspect.signature(target)
                valid_params = [
                    p.name
                    for p in sig.parameters.values()
                    if p.name not in ("self", "args", "kwargs")
                ]
            except Exception:
                pass
            if not valid_params:
                valid_params = extract_params_from_sig(sig_str)
            if valid_params:
                matches = difflib.get_close_matches(invalid_keyword, valid_params, n=5, cutoff=0.5)
                if matches:
                    summary["possible_keywords"] = matches
        except Exception:
            pass

    return summary


def extract_code_excerpt(code, lineno, radius=2):
    if lineno is None:
        return None
    lines = code.splitlines()
    if not lines:
        return None
    index = max(0, lineno - 1)
    start = max(0, index - radius)
    end = min(len(lines), index + radius + 1)
    out = []
    for i in range(start, end):
        prefix = ">>" if i == index else "  "
        out.append("%s %4d | %s" % (prefix, i + 1, lines[i]))
    return "\n".join(out)


def jsonable(value):
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return {
            "repr": repr(value),
            "type": "%s.%s" % (type(value).__module__, type(value).__name__),
        }


# Max serialized size of a successful run_python return_value before we switch
# to a down-sampled preview (see safe_return_value). Keeps a script that sets
# ``result`` to a big array/list from flooding the MCP response and blowing past
# the 16 MiB socket cap in protocol.py. Overridable via env on the kernel side.
try:
    RETURN_VALUE_MAX_BYTES = int(os.environ.get("ABAQUS_MCP_RETURN_MAX", "60000"))
except Exception:
    RETURN_VALUE_MAX_BYTES = 60000


def _json_size(obj):
    """Serialized length in chars, or None if obj is not JSON-serializable."""
    try:
        return len(json.dumps(obj, ensure_ascii=False))
    except (TypeError, ValueError):
        return None


def _downsample_list(seq, max_items=200, max_bytes=8000):
    """Keep a prefix of *seq* within BOTH an item count and a serialized-byte
    budget; report how many were dropped. Returns (preview_list, n_kept, n_dropped).
    An element that cannot be measured is counted at 128 chars (bounded worst case).
    """
    kept = []
    size = 0
    for item in seq:
        if len(kept) >= max_items:
            break
        js = _json_size(item)
        item_size = js if js is not None else min(len(repr(item)), 128)
        if size + item_size > max_bytes:
            break
        kept.append(item)
        size += item_size + 1
    return kept, len(kept), max(0, len(seq) - len(kept))


def safe_return_value(value):
    """Wrap the user's ``result`` for transport, down-sampling oversized payloads.

    Small values pass through unchanged (identical to the old jsonable() path).
    A list/tuple larger than RETURN_VALUE_MAX_BYTES becomes a compact dict::

        {"__truncated__": True, "total": N, "kind": "list", "returned": k,
         "dropped": d, "sample": [...]}

    so an LLM sees shape + head/tail instead of megabytes of numbers. Dicts are
    never silently truncated (that would hide which keys exist); they fall back
    to jsonable(). This runs inside the Abaqus kernel (Py2.7) -- stdlib only, no
    f-strings.
    """
    jv = jsonable(value)
    if isinstance(jv, dict) and "repr" in jv and "type" in jv and len(jv) == 2:
        return jv  # unserializable object -> keep existing {repr,type} envelope
    size = _json_size(jv)
    if size is None or size <= RETURN_VALUE_MAX_BYTES:
        return jv
    if isinstance(value, (list, tuple)):
        total = len(value)
        kind = "list" if isinstance(value, list) else "tuple"
        sample, n_kept, n_dropped = _downsample_list(value)
        return {
            "__truncated__": True,
            "kind": kind,
            "total": total,
            "returned": n_kept,
            "dropped": n_dropped,
            "size_bytes": size,
            "sample": sample,
            "hint": "result too large (%d bytes); set result to a scalar/KPI or "
                    "write to file and return the path. Use offset/limit paging "
                    "via read_odb_field/history for arrays." % size,
        }
    # Oversized but not a flat sequence: keep it, flagging so the caller knows.
    return {
        "__truncated__": True,
        "kind": type(value).__name__,
        "size_bytes": size,
        "preview": repr(value)[:RETURN_VALUE_MAX_BYTES],
        "hint": "result too large to serialize safely; reduce it server-side.",
    }


# --------------------------------------------------------------------------- #
# The top-level error formatter: classifies the exception and builds recovery
# hints. This is the function both agents call after catching a user error.
# --------------------------------------------------------------------------- #
def format_execution_error(code, exc, namespace=None):
    tb_str = traceback.format_exc()
    tb_lines = [l for l in tb_str.strip().splitlines() if l.strip()]
    core_error = tb_lines[-1] if tb_lines else str(exc)
    exc_type_name = type(exc).__name__
    error_type = "%s.%s" % (type(exc).__module__, exc_type_name)
    lineno = extract_tb_lineno(exc)
    code_excerpt = extract_code_excerpt(code, lineno)
    traceback_tail = tb_lines[-5:] if len(tb_lines) > 5 else tb_lines

    is_key_error = isinstance(exc, KeyError) or exc_type_name.endswith("KeyError")
    is_attribute_error = isinstance(exc, AttributeError) or exc_type_name.endswith("AttributeError")
    is_name_error = exc_type_name == "NameError"
    is_type_error = isinstance(exc, TypeError) or exc_type_name.endswith("TypeError")
    is_syntax_error = isinstance(exc, SyntaxError) or exc_type_name.endswith("SyntaxError")

    detected_missing_key = None
    detected_parent_path = None
    if namespace is not None and lineno is not None:
        try:
            lines = code.splitlines()
            if 0 <= lineno - 1 < len(lines):
                line_code = lines[lineno - 1]
                tree = ast.parse(line_code)
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Subscript):
                        continue
                    key_node = unwrap_slice(node.slice)
                    val_key = None
                    if _is_literal(key_node):
                        val_key = literal_value(key_node)
                    elif isinstance(key_node, ast.Name):
                        val_key = namespace.get(key_node.id)
                    if val_key is None:
                        continue
                    p_path = unparse_node(node.value, code)
                    if not p_path:
                        continue
                    try:
                        p_obj = resolve_simple_expr(p_path, namespace)
                        keys_method = getattr(p_obj, "keys", None)
                        if not callable(keys_method):
                            continue
                        try:
                            keys_list = list(keys_method())
                            is_missing = val_key not in keys_list
                        except Exception:
                            is_missing = val_key not in p_obj
                        if is_missing:
                            is_key_error = True
                            detected_missing_key = val_key
                            detected_parent_path = p_path
                            break
                    except Exception:
                        pass
        except Exception:
            pass

    if is_key_error:
        missing_key = detected_missing_key
        if missing_key is None:
            missing_key = exc.args[0] if getattr(exc, "args", None) else None
        parent_path = detected_parent_path
        if parent_path is None:
            parent_path = find_subscript_parent(code, missing_key, lineno)
        recovery = {
            "missing_key": jsonable(missing_key),
            "parent_object_path": parent_path,
        }
        if parent_path and namespace is not None:
            try:
                parent_obj = resolve_simple_expr(parent_path, namespace)
                recovery.update(summarize_mapping_keys(parent_obj, missing_key))
            except Exception:
                pass
    elif is_attribute_error:
        missing_attr = getattr(exc, "name", None)
        source_obj = getattr(exc, "obj", None)
        object_type = type(source_obj).__name__ if source_obj is not None else None
        parent_path = find_attribute_parent(code, missing_attr) if missing_attr else None
        recovery = {
            "missing_attribute": missing_attr,
            "object_type": object_type,
            "parent_object_path": parent_path,
        }
        if source_obj is not None:
            try:
                recovery.update(summarize_object_members(source_obj, missing_attr))
            except Exception:
                pass
    elif is_name_error:
        missing_var = getattr(exc, "name", None)
        if not missing_var:
            m = re.search(r"name '(\w+)' is not defined", str(exc))
            if m:
                missing_var = m.group(1)
        recovery = {"missing_variable": missing_var}
        abaqus_modules = set(
            [
                "mesh", "part", "material", "assembly", "step", "interaction",
                "load", "section", "sketch", "job", "connector", "visualization",
                "xyPlot", "displayGroup", "meshEdit", "connectorBehavior",
                "symbolicConstants",
            ]
        )
        if missing_var in abaqus_modules:
            recovery["import_suggestion"] = "from abaqus import %s" % missing_var
        elif missing_var == "C":
            recovery["import_suggestion"] = "from abaqusConstants import *"
        elif missing_var and missing_var.isupper() and len(missing_var) > 1:
            recovery["import_suggestion"] = "from abaqusConstants import *"
    elif is_syntax_error:
        recovery = {
            "syntax_line": getattr(exc, "lineno", None),
            "syntax_offset": getattr(exc, "offset", None),
            "syntax_text": getattr(exc, "text", None),
        }
    elif is_type_error:
        call_target = extract_call_target(code, lineno)
        invalid_kw = extract_invalid_keyword(core_error)
        recovery = {"call_target": call_target}
        if namespace is not None:
            recovery.update(summarize_callable(call_target, namespace, invalid_kw))
    else:
        recovery = {}

    if not recovery and namespace is not None:
        call_target = extract_call_target(code, lineno)
        if call_target:
            recovery = summarize_callable(call_target, namespace)

    return {
        "ok": False,
        "core_error": core_error,
        "error_type": error_type,
        "error_line": lineno,
        "code_excerpt": code_excerpt,
        "traceback_tail": traceback_tail,
        "recovery": recovery,
    }


def _make_buf():
    """Return a py2/py3 text buffer whose write() accepts both unicode and str.

    Abaqus 2023's embedded CPython 2.7.15 ships a *patched* ``io`` module: even
    a fresh ``io.StringIO()`` raises ``TypeError: unicode argument expected,
    got 'str'`` on byte-str writes (verified live), so every plain ``print``
    blows up once sys.stdout is swapped for it. The stdlib ``StringIO`` class
    tolerates byte-str AND unicode on 2.7, and io.StringIO is correct on 3.x.
    A tiny wrapper decodes bytes defensively either way.
    """
    if sys.version_info[0] < 3:
        try:
            from StringIO import StringIO as _SIO
            return _TolerantBuf(_SIO())
        except Exception:
            pass
    import io as _io
    return _TolerantBuf(_io.StringIO())


class _TolerantBuf(object):
    """Proxy a text buffer, coercing byte-str/bytes writes to unicode on py2.

    Dual-purpose: works standalone (over the kernel's patched stdout) and as
    the execute_in_namespace capture buffer. Inner write failures with a str
    argument are retried after utf-8 decoding, so it survives Abaqus' io where
    even io.StringIO rejects byte-str.
    """

    def __init__(self, inner):
        self._inner = inner

    def write(self, s):
        if sys.version_info[0] < 3 and isinstance(s, str):
            # py2 byte-str: decode so the inner stream never sees raw str.
            # ASCII payloads become unicode (the type patched streams demand);
            # non-ascii byte-str stays as-is because stdlib StringIO accepts it
            # natively -- decoding to unicode would hit the ascii-codec wall on
            # Abaqus' real stdout. The TypeError path below covers that case.
            try:
                s = s.decode("ascii")
            except Exception:
                pass
        elif sys.version_info[0] >= 3 and isinstance(s, bytes):
            s = s.decode("utf-8", "replace")
        try:
            return self._inner.write(s)
        except UnicodeEncodeError:
            # Abaqus' stdout can be an ascii-codec stream: printing a non-ascii
            # literal raises before any type check. Repr keeps capture alive.
            try:
                return self._inner.write(unicode(repr(s)))  # noqa: F821 (py2)
            except Exception:
                try:
                    return self._inner.write(repr(s))
                except Exception:
                    return len(s)
        except TypeError:
            # Stream refuses this shape (patched io wants unicode; stdlib-only
            # paths want str). Try the other shape without re-entering write.
            alt = None
            if isinstance(s, bytes):
                try:
                    alt = s.decode("utf-8", "replace")
                except Exception:
                    alt = None
            elif sys.version_info[0] < 3:
                if isinstance(s, unicode):  # noqa: F821 (py2 builtin)
                    try:
                        alt = s.encode("utf-8", "replace")
                    except Exception:
                        alt = None
                else:
                    try:
                        alt = unicode(s)  # noqa: F821
                    except Exception:
                        alt = None
            else:
                try:
                    alt = u"%s" % (s,)
                except Exception:
                    alt = None
            if alt is not None:
                try:
                    return self._inner.write(alt)
                except Exception:
                    pass
            # repr() yields byte-str on py2 -- patched streams reject THAT too.
            # Force the final fallback to unicode, and give up silently only
            # if even that fails; losing one line beats crashing the whole run.
            try:
                return self._inner.write(unicode(repr(s)))  # noqa: F821
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

    def tell(self):
        return len(self.getvalue())

    def flush(self):
        try:
            self._inner.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self._inner.isatty()
        except Exception:
            return False

    def getvalue(self):
        value = self._inner.getvalue()
        if sys.version_info[0] < 3 and isinstance(value, str):
            try:
                value = value.decode("utf-8", "replace")
            except Exception:
                pass
        return value


def _mcp_probe_stdout_poisoned():
    """True if a fresh io.StringIO on this interpreter rejects byte-str writes.

    Used by BOTH execute_in_namespace and the GUI-plugin kernel header, so the
    detection heuristic can never drift apart between the two copies. Abaqus'
    embedded 2.7 ships a patched io where io.StringIO refuses plain str (and
    sometimes even unicode from print paths); CPython proper never raises here.
    """
    try:
        _probe = io.StringIO()
        _probe.write("ascii")
    except TypeError:
        return True
    except Exception:
        return False
    return False


def execute_in_namespace(code, namespace, lock=None):
    """Run *code* against *namespace*, capture stdout/stderr, return a dict.

    Shared verbatim by both agents. On success returns ok=True with the value
    bound to ``result`` (or the eval'd expression). On failure delegates to
    format_execution_error so the recovery panel is identical everywhere.
    """
    # Abaqus' print machinery bypasses sys.stdout entirely when the kernel's io
    # is patched (verified live: prints reached the poisoned real stdout even
    # with our buffers installed). Pre-empt it: wrap the REAL streams once per
    # call, so every print in any order lands in _TolerantBuf territory.
    if _mcp_probe_stdout_poisoned():
        outer_stdout = _TolerantBuf(sys.stdout)
        outer_stderr = _TolerantBuf(sys.stderr)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = outer_stdout, outer_stderr
        try:
            return _execute_in_namespace_inner(code, namespace, lock)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
    return _execute_in_namespace_inner(code, namespace, lock)


def _execute_in_namespace_inner(code, namespace, lock=None):
    stdout = _make_buf()
    stderr = _make_buf()
    returned = [None]
    error_response = [None]

    set_current_source(code)

    def _run():
        try:
            try:
                parsed = ast.parse(code, mode="eval")
            except SyntaxError:
                parsed = ast.parse(code, mode="exec")
                compiled = compile(parsed, "<abaqus-mcp>", "exec")
                exec(compiled, namespace, namespace)
                returned[0] = namespace.get("result")
            else:
                compiled = compile(parsed, "<abaqus-mcp>", "eval")
                returned[0] = eval(compiled, namespace, namespace)
        except Exception as exc:
            error_response[0] = format_execution_error(code, exc, namespace)

    if lock is not None:
        with lock:
            with _redirect(stdout, stderr):
                _run()
    else:
        with _redirect(stdout, stderr):
            _run()

    captured_stdout = stdout.getvalue()
    captured_stderr = stderr.getvalue()
    cap = 1000
    if len(captured_stdout) > cap:
        captured_stdout = captured_stdout[:cap] + "\n... (truncated, total %d chars)" % len(captured_stdout)
    if len(captured_stderr) > cap:
        captured_stderr = captured_stderr[:cap] + "\n... (truncated, total %d chars)" % len(captured_stderr)

    if error_response[0] is not None:
        error_response[0]["stdout"] = captured_stdout
        error_response[0]["stderr"] = captured_stderr
        return error_response[0]

    payload = safe_return_value(returned[0])
    # Last-resort guard: even the preview can be too big for one socket message.
    if _json_size(payload) is None or _json_size(payload) > RETURN_VALUE_MAX_BYTES * 3:
        payload = {
            "__truncated__": True,
            "kind": type(returned[0]).__name__,
            "hint": "result could not be serialized within the size cap; "
                    "reduce it server-side (return a scalar or write to file).",
        }

    return {
        "ok": True,
        "return_value": payload,
        "stdout": captured_stdout,
        "stderr": captured_stderr,
    }


@contextlib.contextmanager
def _redirect(stdout_buf, stderr_buf):
    """contextlib.redirect_* equivalent that also works on 2.7.

    redirect_stdout arrived in 3.4; Abaqus 2.7 lacks it, so we swap sys.stdout
    / sys.stderr manually. Nesting-safe enough for our single-threaded kernel
    dispatch (all sends are serialised through the GUI dispatcher).
    """
    old_out = sys.stdout
    old_err = sys.stderr
    sys.stdout = stdout_buf
    sys.stderr = stderr_buf
    try:
        yield
    finally:
        sys.stdout = old_out
        sys.stderr = old_err
