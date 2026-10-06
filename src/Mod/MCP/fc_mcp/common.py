# SPDX-License-Identifier: LGPL-2.1-or-later
# ***************************************************************************
# *   This file is part of FreeCAD.                                         *
# *                                                                         *
# *   FreeCAD is free software: you can redistribute it and/or modify it    *
# *   under the terms of the GNU Lesser General Public License as           *
# *   published by the Free Software Foundation, either version 2.1 of the  *
# *   License, or (at your option) any later version.                       *
# *                                                                         *
# *   FreeCAD is distributed in the hope that it will be useful, but        *
# *   WITHOUT ANY WARRANTY; without even the implied warranty of            *
# *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU      *
# *   Lesser General Public License for more details.                       *
# *                                                                         *
# *   You should have received a copy of the GNU Lesser General Public      *
# *   License along with FreeCAD. If not, see                               *
# *   <https://www.gnu.org/licenses/>.                                      *
# *                                                                         *
# ***************************************************************************

"""Definitions shared by the FreeCAD side of the MCP server and the stdio bridge.

This module must only depend on the Python standard library: the bridge runs in
a plain Python interpreter started by the MCP client, without FreeCAD.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sys

SERVER_NAME = "freecad"
SERVER_VERSION = "0.1.0"

# Version of the private protocol spoken between the bridge and FreeCAD.
LINK_PROTOCOL_VERSION = 1

SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[-1]

TOKEN_ENV_VAR = "FREECAD_MCP_TOKEN"
DATA_DIR_ENV_VAR = "FREECAD_MCP_DATA_DIR"
TOKEN_PREFIX = "fcmcp_"

STATUS_FILE = "status.json"
SOCKET_FILE = "mcp.sock"
CLIENTS_DIR = "clients"

# Unix domain socket paths are limited to ~104-108 bytes depending on the OS.
MAX_UNIX_SOCKET_PATH = 100

INSTRUCTIONS = (
    "This server controls a running FreeCAD instance. Use execute_python to run code in "
    "FreeCAD's Python interpreter (the same one used by its Python console); the modules "
    "FreeCAD (App) and FreeCADGui (Gui) are available. Call doc.recompute() after "
    "modifying objects. Use the inspection tools to discover documents and objects."
)

TOOLS = [
    {
        "name": "execute_python",
        "title": "Execute Python in FreeCAD",
        "description": (
            "Execute Python code in the FreeCAD interpreter, in the same namespace as "
            "FreeCAD's Python console. Returns captured stdout/stderr and, if the last "
            "statement is an expression, its repr()."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source code to execute."}
            },
            "required": ["code"],
        },
    },
    {
        "name": "list_documents",
        "title": "List open documents",
        "description": "List the documents open in FreeCAD and which one is active.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_objects",
        "title": "List document objects",
        "description": "List the objects of a document (the active one if omitted).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "document": {"type": "string", "description": "Internal document name."}
            },
        },
    },
    {
        "name": "get_object",
        "title": "Get object properties",
        "description": "Return the properties of a document object.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "document": {"type": "string", "description": "Internal document name."},
                "name": {"type": "string", "description": "Internal object name."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "screenshot",
        "title": "Capture the 3D view",
        "description": "Capture the active 3D view as a PNG image.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {"type": "integer", "minimum": 16, "maximum": 4096},
                "height": {"type": "integer", "minimum": 16, "maximum": 4096},
            },
        },
    },
]


# --------------------------------------------------------------------------
# Tokens


def generate_token():
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token, token_hash):
    if not token or not token_hash:
        return False
    return hmac.compare_digest(hash_token(token), token_hash)


def client_slug(name):
    """Return a file-system friendly identifier for a client name."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name or "").strip("-.")
    return slug[:64] or "client"


# --------------------------------------------------------------------------
# Files shared between FreeCAD and the bridge


def mcp_dir(data_dir):
    return os.path.join(data_dir, "MCP")


def default_data_dir():
    """Best guess of FreeCAD's user data directory when none is given."""
    env = os.environ.get(DATA_DIR_ENV_VAR)
    if env:
        return env
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
        return os.path.join(base, "FreeCAD")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "FreeCAD")
    base = os.environ.get("XDG_DATA_HOME", os.path.join(home, ".local", "share"))
    return os.path.join(base, "FreeCAD")


def ensure_private_dir(path):
    os.makedirs(path, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(path, 0o700)


def write_private_file(path, text):
    """Atomically write a file only readable by the current user."""
    tmp = path + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def unix_socket_path(data_dir):
    """Path of the Unix domain socket, or None on platforms without one."""
    if sys.platform == "win32":
        return None
    path = os.path.join(mcp_dir(data_dir), SOCKET_FILE)
    if len(path.encode("utf-8")) <= MAX_UNIX_SOCKET_PATH:
        return path
    import tempfile

    return os.path.join(tempfile.gettempdir(), "freecad-mcp-{}".format(os.getuid()), SOCKET_FILE)


def read_status(data_dir):
    try:
        with open(os.path.join(mcp_dir(data_dir), STATUS_FILE), encoding="utf-8") as f:
            status = json.load(f)
        return status if isinstance(status, dict) else None
    except (OSError, ValueError):
        return None


def write_status(data_dir, status):
    directory = mcp_dir(data_dir)
    ensure_private_dir(directory)
    write_private_file(os.path.join(directory, STATUS_FILE), json.dumps(status))


def remove_status(data_dir, pid):
    """Remove the status file if it still belongs to process pid."""
    status = read_status(data_dir)
    if status and status.get("pid") == pid:
        try:
            os.remove(os.path.join(mcp_dir(data_dir), STATUS_FILE))
        except OSError:
            pass


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# JSON-RPC helpers


def encode(message):
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def response(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def error_response(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def tool_error(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def initialize_result(requested_version):
    if requested_version in SUPPORTED_PROTOCOL_VERSIONS:
        version = requested_version
    else:
        version = LATEST_PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "title": "FreeCAD", "version": SERVER_VERSION},
        "instructions": INSTRUCTIONS,
    }


PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
