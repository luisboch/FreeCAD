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

"""Preference page of the MCP server (Preferences > Python > MCP)."""

import FreeCAD
from PySide import QtCore, QtGui, QtWidgets

from . import auth, server

translate = FreeCAD.Qt.translate


def _copy(text):
    QtGui.QGuiApplication.clipboard().setText(text)


def show_token_dialog(client_name, token, parent=None):
    """Show the client configuration containing a new token."""
    import FreeCADGui
    from . import clientconfig

    dialog = QtWidgets.QDialog(parent or FreeCADGui.getMainWindow())
    dialog.setWindowTitle(translate("MCP", "MCP client token"))
    dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose)
    layout = QtWidgets.QVBoxLayout(dialog)
    label = QtWidgets.QLabel(
        translate(
            "MCP",
            "Add this server to the configuration of '{}'. The token is shown only "
            "once; FreeCAD only keeps its hash.",
        ).format(client_name)
    )
    label.setWordWrap(True)
    layout.addWidget(label)
    text = QtWidgets.QPlainTextEdit(clientconfig.config_snippet(token))
    text.setReadOnly(True)
    text.setMinimumSize(560, 220)
    layout.addWidget(text)
    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
    copy = buttons.addButton(translate("MCP", "Copy"), QtWidgets.QDialogButtonBox.ActionRole)
    copy.clicked.connect(lambda: _copy(text.toPlainText()))
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    dialog.open()
    return dialog


class MCPPreferencePage:
    def __init__(self):
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle(translate("MCP", "MCP"))
        layout = QtWidgets.QVBoxLayout(self.form)

        general = QtWidgets.QGroupBox(translate("MCP", "Model Context Protocol server"))
        glayout = QtWidgets.QVBoxLayout(general)
        self.enabled = QtWidgets.QCheckBox(
            translate("MCP", "Allow AI agents to connect through MCP")
        )
        self.echo = QtWidgets.QCheckBox(
            translate("MCP", "Show the code executed by agents in the Report view")
        )
        warning = QtWidgets.QLabel(
            translate(
                "MCP",
                "Connected agents can run any Python code in FreeCAD. Each new client "
                "must be authorized when it connects for the first time.",
            )
        )
        warning.setWordWrap(True)
        self.status = QtWidgets.QLabel()
        glayout.addWidget(self.enabled)
        glayout.addWidget(self.echo)
        glayout.addWidget(warning)
        glayout.addWidget(self.status)
        layout.addWidget(general)

        config = QtWidgets.QGroupBox(translate("MCP", "Client configuration"))
        clayout = QtWidgets.QVBoxLayout(config)
        hint = QtWidgets.QLabel(
            translate(
                "MCP",
                "Add this to the configuration of your MCP client. The client starts "
                "FreeCAD if needed. When you choose 'Always allow', the bridge remembers "
                "the authorization; or create a token and keep it in the client "
                "configuration instead.",
            )
        )
        hint.setWordWrap(True)
        self.snippet = QtWidgets.QPlainTextEdit()
        self.snippet.setReadOnly(True)
        self.snippet.setMinimumHeight(140)
        row = QtWidgets.QHBoxLayout()
        copy = QtWidgets.QPushButton(translate("MCP", "Copy"))
        create = QtWidgets.QPushButton(translate("MCP", "Create client token..."))
        row.addWidget(copy)
        row.addWidget(create)
        row.addStretch()
        clayout.addWidget(hint)
        clayout.addWidget(self.snippet)
        clayout.addLayout(row)
        layout.addWidget(config)

        clients = QtWidgets.QGroupBox(translate("MCP", "Authorized clients"))
        tlayout = QtWidgets.QVBoxLayout(clients)
        self.clients = QtWidgets.QTreeWidget()
        self.clients.setRootIsDecorated(False)
        self.clients.setHeaderLabels(
            [
                translate("MCP", "Client"),
                translate("MCP", "Token kept by"),
                translate("MCP", "Authorized"),
                translate("MCP", "Last used"),
            ]
        )
        revoke = QtWidgets.QPushButton(translate("MCP", "Revoke"))
        tlayout.addWidget(self.clients)
        tlayout.addWidget(revoke, 0, QtCore.Qt.AlignLeft)
        layout.addWidget(clients)

        copy.clicked.connect(lambda: _copy(self.snippet.toPlainText()))
        create.clicked.connect(self.create_token)
        revoke.clicked.connect(self.revoke)

    def loadSettings(self):
        grp = server.params()
        self.enabled.setChecked(grp.GetBool("Enabled", False))
        self.echo.setChecked(grp.GetBool("EchoCode", True))
        self.refresh()

    def saveSettings(self):
        grp = server.params()
        grp.SetBool("Enabled", self.enabled.isChecked())
        grp.SetBool("EchoCode", self.echo.isChecked())
        server.apply_preferences()
        self.refresh()

    def refresh(self):
        srv = server.instance()
        if srv is not None and srv.is_listening():
            self.status.setText(translate("MCP", "Status: listening"))
        else:
            self.status.setText(translate("MCP", "Status: stopped"))
        try:
            from . import clientconfig

            self.snippet.setPlainText(clientconfig.config_snippet())
        except Exception as e:
            self.snippet.setPlainText(str(e))
        self.clients.clear()
        modes = {
            auth.MODE_STORED: translate("MCP", "Bridge"),
            auth.MODE_ENV: translate("MCP", "Client configuration"),
        }
        for client in server.registry().list():
            item = QtWidgets.QTreeWidgetItem(
                [
                    client["name"],
                    modes.get(client["mode"], client["mode"]),
                    client["created"],
                    client["last_used"] or "-",
                ]
            )
            item.setData(0, QtCore.Qt.UserRole, client["id"])
            self.clients.addTopLevelItem(item)

    def create_token(self):
        name, ok = QtWidgets.QInputDialog.getText(
            self.form,
            translate("MCP", "Create client token"),
            translate("MCP", "Client name:"),
        )
        name = name.strip()
        if not ok or not name:
            return
        token = server.registry().add(name, auth.MODE_ENV)
        show_token_dialog(name, token, self.form)
        self.refresh()

    def revoke(self):
        item = self.clients.currentItem()
        if item is None:
            return
        client_id = item.data(0, QtCore.Qt.UserRole)
        server.registry().revoke(client_id)
        srv = server.instance()
        if srv is not None:
            srv.disconnect_client(client_id)
        self.refresh()
