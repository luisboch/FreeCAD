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

"""MCP stdio bridge for FreeCAD.

An MCP client starts this script as its server process and talks MCP over
stdin/stdout. The bridge answers the lifecycle requests itself (so the client
does not time out while FreeCAD starts) and forwards everything else to the MCP
server running inside FreeCAD, starting FreeCAD first if it is not running.

Authentication: the bridge presents a token to FreeCAD. The token is taken from
the FREECAD_MCP_TOKEN environment variable (set in the MCP client
configuration) or, failing that, from the token this bridge stored after the
user chose "Always allow" in FreeCAD. Without a valid token FreeCAD asks the
user for permission.

Only the Python standard library is used.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fc_mcp import common
else:
    from . import common


class BridgeError(Exception):
    pass


def log(*args):
    print("[freecad-mcp]", *args, file=sys.stderr, flush=True)


def parent_process_info():
    """Describe the process that started the bridge (normally the MCP client)."""
    ppid = os.getppid()
    info = {"pid": ppid}
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/{}/cmdline".format(ppid), "rb") as f:
                parts = f.read().split(b"\0")
            info["command"] = " ".join(p.decode("utf-8", "replace") for p in parts if p)[:300]
        except OSError:
            pass
    return info


def find_freecad_executable():
    """Locate the FreeCAD GUI executable when it was not given explicitly."""
    for name in ("FreeCAD", "freecad", "FreeCAD.exe"):
        path = shutil.which(name)
        if path:
            return path
    return None


class Link:
    """Connection to the MCP server inside FreeCAD."""

    def __init__(self, sock):
        self.sock = sock
        self.reader = sock.makefile("rb")
        self.write_lock = threading.Lock()

    def send(self, message):
        with self.write_lock:
            self.sock.sendall(common.encode(message))

    def receive(self):
        line = self.reader.readline()
        if not line:
            return None
        return json.loads(line.decode("utf-8"))

    def close(self):
        # shutdown() wakes up a thread blocked in receive(); closing the reader
        # here instead would wait for that thread to release the buffer lock.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def connect_endpoint(status, timeout=2.0):
    if status.get("transport") == "unix":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        address = status.get("path")
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        address = ("127.0.0.1", int(status.get("port", 0)))
    sock.settimeout(timeout)
    try:
        sock.connect(address)
    except (OSError, TypeError, ValueError):
        sock.close()
        return None
    sock.settimeout(None)
    return sock


class Bridge:
    def __init__(
        self,
        data_dir,
        freecad=None,
        launch=True,
        launch_timeout=90.0,
        auth_timeout=300.0,
        stdout=None,
    ):
        self.data_dir = data_dir
        self.freecad = freecad
        self.launch = launch
        self.launch_timeout = launch_timeout
        self.auth_timeout = auth_timeout
        self.stdout = stdout or sys.stdout.buffer
        self.out_lock = threading.Lock()
        self.client_info = {"name": "unknown", "version": ""}
        self.link = None
        self.pending = {}
        self.pending_lock = threading.Lock()

    # ------------------------------------------------------------------ output

    def write(self, message):
        data = common.encode(message)
        with self.out_lock:
            self.stdout.write(data)
            self.stdout.flush()

    # ---------------------------------------------------------- stdin handling

    def run(self, stdin=None):
        stdin = stdin or sys.stdin.buffer
        for raw in stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw.decode("utf-8"))
            except ValueError:
                self.write(common.error_response(None, common.PARSE_ERROR, "Parse error"))
                continue
            self.handle(message)
        if self.link:
            self.link.close()

    def handle(self, message):
        if not isinstance(message, dict):
            self.write(common.error_response(None, common.INVALID_REQUEST, "Invalid request"))
            return
        method = message.get("method")
        msg_id = message.get("id")
        if method is None:
            return  # a response to a request we never send
        if method == "initialize":
            params = message.get("params") or {}
            info = params.get("clientInfo")
            if isinstance(info, dict) and info.get("name"):
                self.client_info = {
                    "name": str(info.get("name")),
                    "version": str(info.get("version", "")),
                }
            result = common.initialize_result(params.get("protocolVersion"))
            self.write(common.response(msg_id, result))
            return
        if "id" not in message:
            return  # notifications (initialized, cancelled, ...) need no answer
        if method == "ping":
            self.write(common.response(msg_id, {}))
            return
        if method == "tools/list":
            self.write(common.response(msg_id, {"tools": common.TOOLS}))
            return
        self.forward(message)

    def forward(self, message):
        msg_id = message.get("id")
        if self.link is None:
            try:
                self.link = self.open_link()
            except BridgeError as e:
                self.fail(message, str(e))
                return
            threading.Thread(target=self.read_link, args=(self.link,), daemon=True).start()
        with self.pending_lock:
            self.pending[json.dumps(msg_id)] = message
        try:
            self.link.send(message)
        except OSError:
            self.drop_link(self.link)

    def fail(self, message, text):
        if message.get("method") == "tools/call":
            self.write(common.response(message.get("id"), common.tool_error(text)))
        else:
            self.write(common.error_response(message.get("id"), common.INTERNAL_ERROR, text))

    def read_link(self, link):
        while True:
            try:
                message = link.receive()
            except (OSError, ValueError):
                message = None
            if message is None:
                break
            if isinstance(message, dict) and "id" in message:
                with self.pending_lock:
                    self.pending.pop(json.dumps(message.get("id")), None)
            self.write(message)
        self.drop_link(link)
        link.reader.close()

    def drop_link(self, link):
        if self.link is not link:
            return
        self.link = None
        link.close()
        with self.pending_lock:
            pending = list(self.pending.values())
            self.pending.clear()
        for message in pending:
            self.fail(message, "The connection to FreeCAD was closed.")

    # ------------------------------------------------- connecting to FreeCAD

    def token_file(self):
        directory = os.path.join(common.mcp_dir(self.data_dir), common.CLIENTS_DIR)
        return os.path.join(directory, common.client_slug(self.client_info["name"]) + ".token")

    def load_token(self):
        token = os.environ.get(common.TOKEN_ENV_VAR, "").strip()
        if token:
            return token, "env"
        try:
            with open(self.token_file(), encoding="utf-8") as f:
                token = f.read().strip()
        except OSError:
            token = ""
        return (token, "stored") if token else (None, None)

    def store_token(self, token):
        path = self.token_file()
        common.ensure_private_dir(common.mcp_dir(self.data_dir))
        common.ensure_private_dir(os.path.dirname(path))
        common.write_private_file(path, token + "\n")

    def open_link(self):
        sock = self.connect_running()
        if sock is None:
            if not self.launch:
                raise BridgeError("FreeCAD is not running.")
            sock = self.launch_and_connect()
        link = Link(sock)
        try:
            self.handshake(link)
        except BaseException:
            link.close()
            raise
        return link

    def connect_running(self):
        status = common.read_status(self.data_dir)
        if not status or not common.pid_alive(status.get("pid")):
            return None
        if not status.get("enabled"):
            raise BridgeError(
                "FreeCAD is running but its MCP server is disabled. Enable it in "
                "Edit > Preferences > Python > MCP."
            )
        for _ in range(3):
            sock = connect_endpoint(status)
            if sock:
                return sock
            time.sleep(0.5)
        return None

    def launch_and_connect(self):
        exe = self.freecad or find_freecad_executable()
        if not exe:
            raise BridgeError(
                "FreeCAD is not running and its executable was not found. "
                "Pass it with --freecad."
            )
        log("starting", exe)
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen([exe], **kwargs)
        except OSError as e:
            raise BridgeError("Could not start FreeCAD ({}): {}".format(exe, e))

        deadline = time.monotonic() + self.launch_timeout
        while time.monotonic() < deadline:
            time.sleep(0.5)
            status = common.read_status(self.data_dir)
            if not status or not common.pid_alive(status.get("pid")):
                continue
            if not status.get("enabled"):
                raise BridgeError(
                    "FreeCAD was started but its MCP server is disabled. Enable it in "
                    "Edit > Preferences > Python > MCP."
                )
            sock = connect_endpoint(status)
            if sock:
                return sock
        raise BridgeError(
            "FreeCAD did not start its MCP server within {:.0f} seconds.".format(
                self.launch_timeout
            )
        )

    def handshake(self, link):
        token, source = self.load_token()
        link.send(
            {
                "type": "hello",
                "link_version": common.LINK_PROTOCOL_VERSION,
                "client": self.client_info,
                "token": token,
                "token_source": source,
                "process": parent_process_info(),
            }
        )
        link.sock.settimeout(self.auth_timeout)
        try:
            reply = link.receive()
        except socket.timeout:
            raise BridgeError("Timed out waiting for the user to authorize this client in FreeCAD.")
        except (OSError, ValueError) as e:
            raise BridgeError("Handshake with FreeCAD failed: {}".format(e))
        link.sock.settimeout(None)

        if not isinstance(reply, dict):
            raise BridgeError("FreeCAD closed the connection during the handshake.")
        if reply.get("type") == "denied":
            raise BridgeError(reply.get("reason") or "FreeCAD denied access to this client.")
        if reply.get("type") != "welcome":
            raise BridgeError("Unexpected handshake reply from FreeCAD.")
        if reply.get("token") and reply.get("persist"):
            try:
                self.store_token(reply["token"])
            except OSError as e:
                log("could not store the token:", e)
        log("connected to FreeCAD")


def main(argv=None):
    parser = argparse.ArgumentParser(description="MCP stdio bridge for FreeCAD")
    parser.add_argument("--data-dir", default=None, help="FreeCAD user data directory")
    parser.add_argument("--freecad", default=None, help="FreeCAD executable to start")
    parser.add_argument(
        "--no-launch", action="store_true", help="never start FreeCAD if it is not running"
    )
    parser.add_argument("--launch-timeout", type=float, default=90.0)
    parser.add_argument("--auth-timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    bridge = Bridge(
        data_dir=args.data_dir or common.default_data_dir(),
        freecad=args.freecad,
        launch=not args.no_launch,
        launch_timeout=args.launch_timeout,
        auth_timeout=args.auth_timeout,
    )
    try:
        bridge.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
