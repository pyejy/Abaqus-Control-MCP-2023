"""Abaqus-side socket agent -- Python 2.7 / 3.x compatible port.

Run this INSIDE Abaqus' bundled interpreter to expose the TCP bridge without
the CAE GUI plugin:

    abaqus python agent.py            # (Abaqus 2023 -> embedded CPython 2.7.15)

It speaks the same line-delimited JSON protocol as the upstream project, so
server.py / client.py on the system Python 3 side are unchanged. All of the
hard execution-diagnostics logic lives in ``diagnostics.py`` (shared with the
GUI plugin); this file only wires up sockets and the namespace.

Standard library only, and only modules present in 2.7: ast, json, platform,
socketserver, sys, threading. No f-strings, no annotations.
"""

import json
import platform
try:
    import socketserver              # Python 3 name
except ImportError:
    import SocketServer as socketserver   # Python 2.7 (Abaqus 2023) name
import sys
import threading

from . import diagnostics as dg

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 48152

_GLOBALS = {
    "__name__": "__abaqus_mcp_exec__",
    "__doc__": None,
}
_EXEC_LOCK = threading.Lock()


def _execute(code):
    # mdb/session are injected when available (only inside a real Abaqus run).
    try:
        from abaqus import mdb, session
    except Exception:
        pass
    else:
        _GLOBALS["mdb"] = mdb
        _GLOBALS["session"] = session
    return dg.execute_in_namespace(code, _GLOBALS, _EXEC_LOCK)


def _ping():
    return {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "thread": threading.current_thread().name,
    }


def _read_message(handler):
    chunks = []
    while True:
        chunk = handler.request.recv(4096)
        if not chunk:
            raise RuntimeError("socket closed before a complete message was received")
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunks.append(chunk[:newline])
            break
        chunks.append(chunk)
    message = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(message, dict):
        raise RuntimeError("protocol message must be a JSON object")
    return message


def _send_message(handler, payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.request.sendall(data + b"\n")


class AbaqusMcpHandler(socketserver.BaseRequestHandler):
    def handle(self):
        request_id = None
        try:
            message = _read_message(self)
            request_id = message.get("id")
            method = message.get("method")
            params = message.get("params") or {}

            if method == "ping":
                result = _ping()
            elif method == "execute":
                code = params.get("code")
                if not isinstance(code, str) or not code.strip():
                    raise ValueError("params.code must be a non-empty string")
                result = _execute(code)
            else:
                raise ValueError("unknown method: %r" % method)

            _send_message(self, {"id": request_id, "ok": True, "result": result})
        except Exception as exc:
            _send_message(
                self,
                {
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "message": str(exc),
                        "type": "%s.%s" % (type(exc).__module__, type(exc).__name__),
                        "traceback": __import__("traceback").format_exc(),
                    },
                },
            )


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(host=DEFAULT_HOST, port=DEFAULT_PORT):
    server = ThreadedTCPServer((host, port), AbaqusMcpHandler)
    print("Abaqus MCP agent listening on %s:%s" % (host, port))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
