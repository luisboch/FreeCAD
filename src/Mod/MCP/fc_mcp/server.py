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

"""MCP server running inside the FreeCAD GUI.

The server listens on a Unix domain socket only accessible by the current user
(a loopback TCP port on Windows) and is reached through the stdio bridge. Each
connection starts with a handshake in which the bridge presents a token; unknown
clients must be authorized by the user. After the handshake, the connection
carries MCP JSON-RPC messages, one per line.
"""

import json
import os
import sys

import FreeCAD
from PySide import QtCore, QtNetwork

from . import auth, common

translate = FreeCAD.Qt.translate

PARAM_PATH = "User parameter:BaseApp/Preferences/MCP"

# Consent decisions
DENY = "deny"
ALLOW_ONCE = "once"
ALLOW_STORED = auth.MODE_STORED
ALLOW_ENV = auth.MODE_ENV


def params():
    return FreeCAD.ParamGet(PARAM_PATH)


def registry():
    return auth.ClientRegistry(params().GetGroup("Clients"))


class Session(QtCore.QObject):
    """One connection from a bridge."""

    def __init__(self, server, socket):
        super().__init__(server)
        self.server = server
        self.socket = socket
        self.buffer = b""
        self.queue = []
        self.busy = False
        self.state = "hello"
        self.client = {"name": "unknown", "version": ""}
        self.client_id = None
        socket.readyRead.connect(self._on_ready_read)
        socket.disconnected.connect(self._on_disconnected)

    # ------------------------------------------------------------------ I/O

    def send(self, message):
        if self.state != "closed":
            self.socket.write(common.encode(message))
            self.socket.flush()

    def close(self):
        if self.state == "closed":
            return
        self.state = "closed"
        self.server.session_closed(self)
        if isinstance(self.socket, QtNetwork.QLocalSocket):
            self.socket.disconnectFromServer()
        else:
            self.socket.disconnectFromHost()

    def _on_disconnected(self):
        if self.state != "closed":
            self.state = "closed"
            self.server.session_closed(self)
        self.socket.deleteLater()

    def _on_ready_read(self):
        self.buffer += bytes(self.socket.readAll())
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if line.strip():
                self.queue.append(line)
        if self.busy:
            return  # re-entered through processEvents() while running a tool
        self.busy = True
        try:
            while self.queue and self.state in ("hello", "ready"):
                self._process(self.queue.pop(0))
        finally:
            self.busy = False

    def _process(self, line):
        try:
            message = json.loads(line.decode("utf-8"))
        except ValueError:
            if self.state == "hello":
                self.close()
            else:
                self.send(common.error_response(None, common.PARSE_ERROR, "Parse error"))
            return
        if self.state == "hello":
            self._handshake(message)
        else:
            self._dispatch(message)

    # ------------------------------------------------------------ handshake

    def _handshake(self, hello):
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            self.close()
            return
        if hello.get("link_version") != common.LINK_PROTOCOL_VERSION:
            self.send({"type": "denied", "reason": "Incompatible FreeCAD MCP bridge version."})
            self.close()
            return
        client = hello.get("client")
        if isinstance(client, dict):
            self.client = {
                "name": str(client.get("name", "unknown"))[:100],
                "version": str(client.get("version", ""))[:50],
            }
        reg = registry()
        gid = reg.find(hello.get("token"))
        if gid:
            reg.touch(gid)
            self.client_id = gid
            self._accept({"type": "welcome"})
            return
        self.state = "waiting"
        self.server.request_consent(self, hello)

    def consent_given(self, decision):
        if self.state != "waiting":
            return
        if decision == DENY:
            self.send({"type": "denied", "reason": "The user denied access in FreeCAD."})
            self.close()
            return
        welcome = {"type": "welcome"}
        if decision in (ALLOW_STORED, ALLOW_ENV):
            reg = registry()
            token = reg.add(self.client["name"], decision)
            self.client_id = reg.find(token)
            if decision == ALLOW_STORED:
                welcome.update(token=token, persist=True)
            else:
                self.server.show_token(self.client["name"], token)
        self._accept(welcome)

    def _accept(self, welcome):
        self.state = "ready"
        self.send(welcome)
        FreeCAD.Console.PrintLog("MCP: client '{}' connected\n".format(self.client["name"]))
        # The bridge may have sent requests right after the hello.
        if self.queue and not self.busy:
            self._on_ready_read()

    # ------------------------------------------------------------- JSON-RPC

    def _dispatch(self, message):
        if not isinstance(message, dict) or "method" not in message:
            return
        if "id" not in message:
            return  # notification
        msg_id = message.get("id")
        method = message.get("method")
        msg_params = message.get("params") or {}
        if method == "initialize":
            result = common.initialize_result(msg_params.get("protocolVersion"))
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": common.TOOLS}
        elif method == "tools/call":
            from . import tools

            result = tools.call_tool(
                msg_params.get("name"), msg_params.get("arguments"), self.client["name"]
            )
        else:
            self.send(
                common.error_response(
                    msg_id, common.METHOD_NOT_FOUND, "Method not found: {}".format(method)
                )
            )
            return
        self.send(common.response(msg_id, result))


class Server(QtCore.QObject):
    def __init__(self, data_dir, consent=None, token_presenter=None, parent=None):
        """consent(session, hello, callback) asks the user and calls callback(decision).
        token_presenter(client_name, token) shows a token to put in the client config.
        """
        super().__init__(parent)
        self.data_dir = data_dir
        self.consent = consent or ask_consent
        self.token_presenter = token_presenter or present_token
        self.listener = None
        self.sessions = []
        self.status = None

    def start(self):
        if self.listener is not None:
            return True
        other = common.read_status(self.data_dir)
        if (
            other
            and other.get("enabled")
            and other.get("pid") != os.getpid()
            and common.pid_alive(other.get("pid"))
        ):
            FreeCAD.Console.PrintWarning(
                "MCP: another FreeCAD instance (PID {}) already serves MCP\n".format(
                    other.get("pid")
                )
            )
            return False

        common.ensure_private_dir(common.mcp_dir(self.data_dir))
        if sys.platform == "win32":
            listener = QtNetwork.QTcpServer(self)
            ok = listener.listen(QtNetwork.QHostAddress(QtNetwork.QHostAddress.LocalHost), 0)
            status = {"transport": "tcp", "port": listener.serverPort()}
        else:
            path = common.unix_socket_path(self.data_dir)
            common.ensure_private_dir(os.path.dirname(path))
            QtNetwork.QLocalServer.removeServer(path)
            listener = QtNetwork.QLocalServer(self)
            listener.setSocketOptions(QtNetwork.QLocalServer.UserAccessOption)
            ok = listener.listen(path)
            status = {"transport": "unix", "path": path}
        if not ok:
            FreeCAD.Console.PrintError(
                "MCP: could not start the server: {}\n".format(listener.errorString())
            )
            return False
        listener.newConnection.connect(self._on_new_connection)
        self.listener = listener
        status.update(pid=os.getpid(), enabled=True, link_version=common.LINK_PROTOCOL_VERSION)
        self.status = status
        common.write_status(self.data_dir, status)
        FreeCAD.Console.PrintLog("MCP: server listening ({})\n".format(status["transport"]))
        return True

    def stop(self):
        for session in list(self.sessions):
            session.close()
        if self.listener is not None:
            self.listener.close()
            self.listener.deleteLater()
            self.listener = None
            common.remove_status(self.data_dir, os.getpid())
        self.status = None

    def is_listening(self):
        return self.listener is not None

    def _on_new_connection(self):
        while self.listener.hasPendingConnections():
            socket = self.listener.nextPendingConnection()
            self.sessions.append(Session(self, socket))

    def session_closed(self, session):
        if session in self.sessions:
            self.sessions.remove(session)

    def request_consent(self, session, hello):
        self.consent(session, hello, session.consent_given)

    def show_token(self, client_name, token):
        self.token_presenter(client_name, token)

    def disconnect_client(self, client_id):
        for session in list(self.sessions):
            if session.client_id == client_id:
                session.close()


# --------------------------------------------------------------------------
# User interface


def ask_consent(session, hello, callback):
    from PySide import QtWidgets
    import FreeCADGui

    client = session.client
    process = hello.get("process") or {}
    lines = [
        translate("MCP", "An AI agent wants to control FreeCAD through MCP."),
        "",
        translate("MCP", "Client: {}").format((client["name"] + " " + client["version"]).strip()),
    ]
    if process.get("command"):
        lines.append(
            translate("MCP", "Started by: {} (PID {})").format(
                process.get("command"), process.get("pid")
            )
        )
    if hello.get("token"):
        lines += ["", translate("MCP", "The client presented a token that is not authorized.")]
    lines += [
        "",
        translate(
            "MCP",
            "If you allow it, the agent can run any Python code in FreeCAD with your "
            "user permissions, including reading and writing files.",
        ),
    ]

    mw = FreeCADGui.getMainWindow()
    box = QtWidgets.QMessageBox(mw)
    box.setIcon(QtWidgets.QMessageBox.Warning)
    box.setWindowTitle(translate("MCP", "MCP connection request"))
    box.setText("\n".join(lines))
    check = QtWidgets.QCheckBox(
        translate(
            "MCP",
            "Remember with a token I add to the client configuration ({})",
        ).format(common.TOKEN_ENV_VAR)
    )
    check.setChecked(hello.get("token_source") == "env")
    box.setCheckBox(check)
    deny = box.addButton(translate("MCP", "Deny"), QtWidgets.QMessageBox.RejectRole)
    once = box.addButton(translate("MCP", "Allow once"), QtWidgets.QMessageBox.AcceptRole)
    always = box.addButton(translate("MCP", "Always allow"), QtWidgets.QMessageBox.AcceptRole)
    box.setDefaultButton(deny)
    box.setEscapeButton(deny)
    box.setAttribute(QtCore.Qt.WA_DeleteOnClose)

    def finished():
        clicked = box.clickedButton()
        if clicked is always:
            callback(ALLOW_ENV if check.isChecked() else ALLOW_STORED)
        elif clicked is once:
            callback(ALLOW_ONCE)
        else:
            callback(DENY)

    box.finished.connect(lambda _result: finished())
    # Close the question if the bridge goes away (for instance, its timeout).
    session.socket.disconnected.connect(box.reject)
    if mw.isMinimized():
        mw.showNormal()
    mw.raise_()
    mw.activateWindow()
    box.open()


def present_token(client_name, token):
    from . import preferences

    preferences.show_token_dialog(client_name, token)


# --------------------------------------------------------------------------
# Module level instance


_server = None


def instance():
    return _server


def _data_dir():
    return FreeCAD.getUserAppDataDir()


def is_enabled():
    return params().GetBool("Enabled", False)


def _write_disabled_status():
    # Lets the bridge tell "FreeCAD runs with MCP disabled" from "FreeCAD is not
    # running". Only done once MCP has been used, to leave no trace otherwise.
    data_dir = _data_dir()
    if os.path.isdir(common.mcp_dir(data_dir)):
        common.write_status(data_dir, {"pid": os.getpid(), "enabled": False})


def apply_preferences():
    """Start or stop the server according to the preferences."""
    global _server
    if is_enabled():
        if _server is None:
            _server = Server(_data_dir())
        _server.start()
    else:
        if _server is not None:
            _server.stop()
        _write_disabled_status()


def _on_quit():
    if _server is not None:
        _server.stop()
    common.remove_status(_data_dir(), os.getpid())


def startup():
    from PySide import QtWidgets

    app = QtWidgets.QApplication.instance()
    if app is not None:
        app.aboutToQuit.connect(_on_quit)
    try:
        apply_preferences()
    except Exception as e:
        FreeCAD.Console.PrintError("MCP: {}\n".format(e))
