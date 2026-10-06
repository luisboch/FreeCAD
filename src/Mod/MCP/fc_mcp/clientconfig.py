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

"""Builds the configuration MCP clients use to start the bridge."""

import json
import os
import shutil
import sys

import FreeCAD

from . import common

BRIDGE_FILES = ("__init__.py", "common.py", "bridge.py")


def data_dir():
    return FreeCAD.getUserAppDataDir()


def install_bridge():
    """Copy the bridge to the user data directory and return the script path.

    The copy keeps the path in the client configuration valid across FreeCAD
    upgrades and for AppImages, whose mount point changes on each start.
    """
    target = os.path.join(common.mcp_dir(data_dir()), "bridge", "fc_mcp")
    os.makedirs(target, exist_ok=True)
    source = os.path.dirname(os.path.abspath(__file__))
    for name in BRIDGE_FILES:
        src = os.path.join(source, name)
        dst = os.path.join(target, name)
        with open(src, "rb") as f:
            content = f.read()
        try:
            with open(dst, "rb") as f:
                if f.read() == content:
                    continue
        except OSError:
            pass
        with open(dst, "wb") as f:
            f.write(content)
    return os.path.join(target, "bridge.py")


def _first_existing(paths):
    for path in paths:
        if path and os.path.isfile(path):
            return path
    return None


def python_executable():
    """A Python interpreter able to run the bridge (it only needs the stdlib)."""
    if not os.environ.get("APPIMAGE"):
        bindir = os.path.join(FreeCAD.getHomePath(), "bin")
        names = ("python.exe", "python3.exe") if sys.platform == "win32" else ("python3", "python")
        found = _first_existing(os.path.join(bindir, n) for n in names)
        if found:
            return found
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return "python3"


def freecad_executable():
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return appimage
    home = FreeCAD.getHomePath()
    candidates = [
        os.path.join(home, "bin", "FreeCAD.exe"),
        os.path.join(home, "bin", "FreeCAD"),
        os.path.join(home, "bin", "freecad"),
        os.path.join(home, "MacOS", "FreeCAD"),
    ]
    found = _first_existing(candidates)
    if found:
        return found
    if os.path.basename(sys.executable).lower().startswith("freecad"):
        return sys.executable
    return shutil.which("freecad") or shutil.which("FreeCAD") or "FreeCAD"


def server_entry(token=None):
    entry = {
        "command": python_executable(),
        "args": [install_bridge(), "--data-dir", data_dir(), "--freecad", freecad_executable()],
    }
    if token:
        entry["env"] = {common.TOKEN_ENV_VAR: token}
    return entry


def config_snippet(token=None):
    return json.dumps({"mcpServers": {"freecad": server_entry(token)}}, indent=2)
