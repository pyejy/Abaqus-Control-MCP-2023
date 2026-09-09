"""Entry point for the abaqus MCP bridge (path-anchored).

Lets Claude Code start this server with an absolute path, so it always imports
the ``abaqus_mcp_bridge`` package that lives right next to this file -- even if
the process working directory is somewhere else. No editable install required.
Keeps 2.7-safe server code untouched; this small wrapper is Python-3 only.
"""

import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    sys.path.insert(0, _HERE)
    # Drop the first sys.argv[0] (this script) and run the real server as the
    # blessed ``-m`` entry so FastMCP's stdio startup behaves normally.
    from abaqus_mcp_bridge import server  # noqa: PLC0415

    server.main()


if __name__ == "__main__":
    main()