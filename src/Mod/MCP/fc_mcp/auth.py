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

"""Registry of the MCP clients the user authorized permanently.

Only the SHA-256 hash of each token is kept, in FreeCAD's user parameters under
User parameter:BaseApp/Preferences/MCP/Clients/<id>.
"""

import datetime

from . import common

# How the client keeps its token.
MODE_STORED = "stored"  # saved by the bridge in the user data directory
MODE_ENV = "env"  # set by the user in the client configuration (FREECAD_MCP_TOKEN)


def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


class ClientRegistry:
    def __init__(self, group):
        """group: a FreeCAD ParameterGrp (or compatible object)."""
        self.group = group

    def find(self, token):
        """Return the id of the client owning token, or None."""
        if not token:
            return None
        for gid in self.group.GetGroups():
            sub = self.group.GetGroup(gid)
            if common.token_matches(token, sub.GetString("TokenHash", "")):
                return gid
        return None

    def add(self, name, mode):
        """Authorize a new client and return its (clear text) token."""
        token = common.generate_token()
        token_hash = common.hash_token(token)
        gid = "Client_" + token_hash[:12]
        sub = self.group.GetGroup(gid)
        sub.SetString("Name", name)
        sub.SetString("Mode", mode)
        sub.SetString("TokenHash", token_hash)
        sub.SetString("Created", _now())
        sub.SetString("LastUsed", "")
        return token

    def touch(self, gid):
        self.group.GetGroup(gid).SetString("LastUsed", _now())

    def get(self, gid):
        sub = self.group.GetGroup(gid)
        return {
            "id": gid,
            "name": sub.GetString("Name", gid),
            "mode": sub.GetString("Mode", MODE_STORED),
            "created": sub.GetString("Created", ""),
            "last_used": sub.GetString("LastUsed", ""),
        }

    def list(self):
        return [self.get(gid) for gid in self.group.GetGroups()]

    def revoke(self, gid):
        self.group.RemGroup(gid)
