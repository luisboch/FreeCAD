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

"""Unit tests of the MCP module that do not need the GUI."""

import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

import FreeCAD

from fc_mcp import auth, bridge, common


class TestTokens(unittest.TestCase):
    def test_token_hash(self):
        token = common.generate_token()
        self.assertTrue(token.startswith(common.TOKEN_PREFIX))
        self.assertNotEqual(token, common.generate_token())
        token_hash = common.hash_token(token)
        self.assertTrue(common.token_matches(token, token_hash))
        self.assertFalse(common.token_matches(token + "x", token_hash))
        self.assertFalse(common.token_matches("", token_hash))
        self.assertFalse(common.token_matches(token, ""))

    def test_client_slug(self):
        self.assertEqual(common.client_slug("claude-code"), "claude-code")
        self.assertEqual(common.client_slug("../../etc/passwd"), "etc-passwd")
        self.assertEqual(common.client_slug(""), "client")

    def test_initialize_result(self):
        result = common.initialize_result("2025-06-18")
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", result["capabilities"])
        result = common.initialize_result("1999-01-01")
        self.assertEqual(result["protocolVersion"], common.LATEST_PROTOCOL_VERSION)


class TestClientRegistry(unittest.TestCase):
    PATH = "User parameter:BaseApp/Preferences/TestMCP"

    def setUp(self):
        self.registry = auth.ClientRegistry(FreeCAD.ParamGet(self.PATH).GetGroup("Clients"))

    def tearDown(self):
        FreeCAD.ParamGet("User parameter:BaseApp/Preferences").RemGroup("TestMCP")

    def test_add_find_revoke(self):
        token = self.registry.add("claude-code", auth.MODE_STORED)
        gid = self.registry.find(token)
        self.assertIsNotNone(gid)
        self.assertIsNone(self.registry.find("fcmcp_unknown"))
        self.assertIsNone(self.registry.find(None))
        client = self.registry.get(gid)
        self.assertEqual(client["name"], "claude-code")
        self.assertEqual(client["mode"], auth.MODE_STORED)
        # Only the hash is stored.
        self.assertNotIn(token, json.dumps(self.registry.list()))
        self.assertNotEqual(
            FreeCAD.ParamGet(self.PATH).GetGroup("Clients").GetGroup(gid).GetString("TokenHash"),
            token,
        )
        self.registry.touch(gid)
        self.assertTrue(self.registry.get(gid)["last_used"])
        self.registry.revoke(gid)
        self.assertIsNone(self.registry.find(token))


class FakeFreeCAD:
    """A minimal stand-in for the MCP server inside FreeCAD."""

    def __init__(self, path, token_to_issue):
        self.path = path
        self.token_to_issue = token_to_issue
        self.hellos = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(4)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            reader = conn.makefile("rb")
            hello = json.loads(reader.readline())
            self.hellos.append(hello)
            welcome = {"type": "welcome"}
            if not hello.get("token"):
                welcome.update(token=self.token_to_issue, persist=True)
            conn.sendall(common.encode(welcome))
            for line in reader:
                message = json.loads(line)
                result = {"content": [{"type": "text", "text": "ok"}]}
                conn.sendall(common.encode(common.response(message["id"], result)))
            conn.close()

    def close(self):
        self.sock.close()


class LockedBytesIO(io.BytesIO):
    def messages(self):
        return [json.loads(line) for line in self.getvalue().splitlines()]


def run_bridge(bridge_obj, *messages):
    data = b"".join(common.encode(m) for m in messages)
    bridge_obj.run(io.BytesIO(data))


def call_and_wait(bridge_obj, out, message, timeout=10.0):
    """Forward message and wait for its answer before closing the link."""
    bridge_obj.handle(message)
    deadline = time.monotonic() + timeout
    while not out.getvalue() and time.monotonic() < deadline:
        time.sleep(0.01)
    if bridge_obj.link:
        bridge_obj.link.close()
    return out.messages()[0]


class TestBridge(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="fcmcp")
        self.saved_env = os.environ.pop(common.TOKEN_ENV_VAR, None)

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)
        if self.saved_env is not None:
            os.environ[common.TOKEN_ENV_VAR] = self.saved_env

    def make_bridge(self):
        out = LockedBytesIO()
        return bridge.Bridge(self.data_dir, launch=False, stdout=out), out

    def test_lifecycle_is_answered_locally(self):
        b, out = self.make_bridge()
        run_bridge(
            b,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "clientInfo": {"name": "test-client", "version": "1"},
                    "capabilities": {},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        )
        replies = out.messages()
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "freecad")
        names = [t["name"] for t in replies[1]["result"]["tools"]]
        self.assertIn("execute_python", names)
        self.assertEqual(b.client_info["name"], "test-client")

    def test_tool_call_without_freecad(self):
        b, out = self.make_bridge()
        run_bridge(
            b,
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "x"}},
        )
        reply = out.messages()[0]
        self.assertEqual(reply["id"], 7)
        self.assertTrue(reply["result"]["isError"])

    def test_disabled_freecad(self):
        common.write_status(self.data_dir, {"pid": os.getpid(), "enabled": False})
        b, out = self.make_bridge()
        run_bridge(b, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
        reply = out.messages()[0]
        self.assertIn("disabled", reply["error"]["message"])

    @unittest.skipIf(sys.platform == "win32", "uses a Unix domain socket")
    def test_token_is_stored_and_reused(self):
        path = common.unix_socket_path(self.data_dir)
        common.ensure_private_dir(os.path.dirname(path))
        fake = FakeFreeCAD(path, "fcmcp_issued")
        try:
            common.write_status(
                self.data_dir,
                {"pid": os.getpid(), "enabled": True, "transport": "unix", "path": path},
            )
            call = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "x"}}

            b, out = self.make_bridge()
            reply = call_and_wait(b, out, call)
            self.assertEqual(reply["result"]["content"][0]["text"], "ok")
            self.assertIsNone(fake.hellos[0]["token"])
            token_file = b.token_file()
            with open(token_file) as f:
                self.assertEqual(f.read().strip(), "fcmcp_issued")
            self.assertEqual(os.stat(token_file).st_mode & 0o777, 0o600)

            b, out = self.make_bridge()
            call_and_wait(b, out, call)
            self.assertEqual(fake.hellos[1]["token"], "fcmcp_issued")
            self.assertEqual(fake.hellos[1]["token_source"], "stored")

            os.environ[common.TOKEN_ENV_VAR] = "fcmcp_from_env"
            b, out = self.make_bridge()
            call_and_wait(b, out, call)
            self.assertEqual(fake.hellos[2]["token"], "fcmcp_from_env")
            self.assertEqual(fake.hellos[2]["token_source"], "env")
        finally:
            os.environ.pop(common.TOKEN_ENV_VAR, None)
            fake.close()
