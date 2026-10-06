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

"""MCP module: lets AI agents control FreeCAD through the Model Context Protocol.

The server is disabled by default (Preferences > Python > MCP).
"""


def _setup_mcp():
    from PySide import QtCore

    from fc_mcp import preferences, server

    FreeCADGui.addPreferencePage(
        preferences.MCPPreferencePage, FreeCAD.Qt.QT_TRANSLATE_NOOP("QObject", "Python")
    )
    # Start once the main window is up.
    QtCore.QTimer.singleShot(0, server.startup)


_setup_mcp()
