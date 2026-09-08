"""Standalone 2.7-safe smoke test for the ported diagnostics.

Run INSIDE Abaqus' interpreter::

    abaqus python tests/smoke_diagnostics.py

It must import and run under CPython 2.7.15 with NO project deps and NO mcp.
Prints a PASS line on success; raises (nonzero exit) on any regression.
Deliberately avoids py3-only syntax so it doubles as a live compatibility proof.
"""

import os
import sys

# Make the package importable when run from anywhere.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from abaqus_mcp_bridge import diagnostics as dg


def _expect(condition, label):
    if not condition:
        raise AssertionError("FAILED: " + label)
    print("  ok - " + label)


print("python running this test:", sys.version.split()[0])

# 1) Literal / slice helpers work on whatever AST this interpreter exposes.
import ast
tree = ast.parse("parts['braket']")
sub = None
for node in ast.walk(tree):
    if isinstance(node, ast.Subscript):
        sub = node
_key = dg.key_literal(sub.slice)
_expect(_key == "braket", "key_literal unwraps subscript key -> %r" % (_key,))

# 2) unparse fallback recovers source text even without ast.unparse (2.7).
#    On 2.7 there are no end coordinates, so the physical-line slice of a
#    call.func node runs to the end of the line -- it yields the callee chain
#    plus any trailing call arguments ("my_obj.some_method(a, b)"). Downstream
#    recovery (find_subscript_parent / find_attribute_parent / call_target) is
#    built to tolerate that. Assert the callee prefix survives, which is what
#    matters for the diagnostic hints.
src = "x = my_obj.some_method(a, b)"
t2 = ast.parse(src)
call = None
for node in ast.walk(t2):
    if isinstance(node, ast.Call):
        call = node
recovered = dg.unparse_node(call.func, src)
_expect(recovered is not None and recovered.startswith("my_obj.some_method"),
        "unparse_node recovered callee prefix -> %r" % recovered)

# 3) Full error recovery panel via execute_in_namespace.
ns = {"parts": {"Bracket": 1, "Base": 2}}
res = dg.execute_in_namespace("result = parts['braket']", ns)
_expect(res.get("ok") is False, "execute_in_namespace flags failure")
rec = res.get("recovery", {})
_expect(rec.get("missing_key") == "braket", "missing_key detected")
_expect("Bracket" in (rec.get("possible_keys") or []),
        "possible_keys suggests 'Bracket' -> %r" % (rec.get("possible_keys"),))

# 4) NameError on an Abaqus module name yields an import suggestion.
res2 = dg.execute_in_namespace("mesh.Box()", {})
rec2 = res2.get("recovery", {})
_expect(rec2.get("import_suggestion") == "from abaqus import mesh",
        "NameError import suggestion -> %r" % rec2.get("import_suggestion"))

# 5) Happy path returns the value bound to `result`.
res3 = dg.execute_in_namespace("result = 6 * 7", {})
_expect(res3.get("ok") is True and res3.get("return_value") == 42,
        "success path return_value -> %r" % res3.get("return_value"))

print("\nALL SMOKE CHECKS PASSED on Python %s" % sys.version.split()[0])
