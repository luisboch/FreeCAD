# MCP module

Lets AI agents control FreeCAD through the
[Model Context Protocol](https://modelcontextprotocol.io). It is **disabled by
default**: enable it in *Edit > Preferences > Python > MCP*.

## How it works

```
MCP client ──stdio──> bridge.py ──local socket──> MCP server inside FreeCAD
```

* MCP clients start servers as processes and talk to them over stdin/stdout.
  `fc_mcp/bridge.py` is that process. It only uses the Python standard library.
  It answers `initialize`, `tools/list` and `ping` itself and forwards the rest
  to FreeCAD, **starting FreeCAD first if it is not running**.
* FreeCAD listens on a Unix domain socket only accessible by the current user
  (a loopback TCP port on Windows). `<UserAppData>/MCP/status.json` tells the
  bridge where, and whether MCP is enabled.
* Tools run in the GUI thread, in the namespace of the Python console:
  `execute_python`, `list_documents`, `list_objects`, `get_object`, `screenshot`.
  Each `execute_python` call is one undo transaction of the active document.

## Authorization

The MCP specification leaves authorization of stdio servers to credentials taken
from the environment, so the bridge presents a token to FreeCAD when it
connects:

1. `FREECAD_MCP_TOKEN` from the environment, set in the client configuration, or
2. the token the bridge stored in `<UserAppData>/MCP/clients/<client>.token`
   (mode 0600).

Without a valid token FreeCAD asks the user: *Deny*, *Allow once* or *Always
allow*. *Always allow* creates a token, stores only its SHA-256 hash in the user
parameters (`BaseApp/Preferences/MCP/Clients`) and either hands it to the bridge
to store or, when "Remember with a token I add to the client configuration" is
checked, shows the client configuration containing it. Authorized clients are
listed, and can be revoked, in the preference page.

The token proves the user consented; it is not a barrier against other programs
running as the same user, which can read the stored token.

## Client configuration

The preference page shows the configuration to copy, for example:

```json
{
  "mcpServers": {
    "freecad": {
      "command": "/usr/bin/python3",
      "args": [
        "~/.local/share/FreeCAD/MCP/bridge/fc_mcp/bridge.py",
        "--data-dir", "~/.local/share/FreeCAD",
        "--freecad", "/usr/bin/FreeCAD"
      ]
    }
  }
}
```

The bridge is copied to the user data directory so this path stays valid across
FreeCAD updates and AppImage mounts.
