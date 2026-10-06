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

"""Implementation of the MCP tools. Runs in FreeCAD's GUI thread."""

import ast
import base64
import contextlib
import io
import json
import os
import tempfile
import traceback

import FreeCAD

from . import common

MAX_REPR = 2000


def _text(text, is_error=False):
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _json(data):
    return _text(json.dumps(data, indent=1, default=str))


def _short_repr(value, limit=MAX_REPR):
    try:
        text = repr(value)
    except Exception as e:
        text = "<unrepresentable: {}>".format(e)
    if len(text) > limit:
        text = text[:limit] + "...<truncated>"
    return text


def _document(name):
    if name:
        doc = FreeCAD.listDocuments().get(name)
        if doc is None:
            raise ValueError("No open document named '{}'.".format(name))
        return doc
    if FreeCAD.ActiveDocument is None:
        raise ValueError("There is no active document.")
    return FreeCAD.ActiveDocument


def execute_python(code, client_name=""):
    if not isinstance(code, str):
        raise ValueError("'code' must be a string.")
    params = FreeCAD.ParamGet("User parameter:BaseApp/Preferences/MCP")
    if params.GetBool("EchoCode", True):
        FreeCAD.Console.PrintMessage(
            "MCP ({}) executing:\n{}\n".format(client_name or "client", code.rstrip())
        )

    import __main__

    namespace = __main__.__dict__
    out = io.StringIO()
    err = io.StringIO()
    value_repr = None
    failed = False

    doc = FreeCAD.ActiveDocument
    if doc is not None:
        doc.openTransaction("MCP")
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                tree = ast.parse(code, "<mcp>", "exec")
                last = None
                if tree.body and isinstance(tree.body[-1], ast.Expr):
                    last = ast.Expression(tree.body.pop().value)
                exec(compile(tree, "<mcp>", "exec"), namespace)
                if last is not None:
                    value = eval(compile(last, "<mcp>", "eval"), namespace)
                    if value is not None:
                        value_repr = _short_repr(value, 20000)
            except (Exception, SystemExit) as e:
                failed = True
                # Hide this module's frame from the traceback.
                tb = e.__traceback__.tb_next if e.__traceback__ else None
                traceback.print_exception(type(e), e, tb, file=err)
    finally:
        if doc is not None and doc in FreeCAD.listDocuments().values():
            doc.commitTransaction()

    parts = []
    if out.getvalue():
        parts.append("stdout:\n" + out.getvalue())
    if err.getvalue():
        parts.append("stderr:\n" + err.getvalue())
    if value_repr is not None:
        parts.append("result:\n" + value_repr)
    if not parts:
        parts.append("(no output)")
    return _text("\n".join(parts), failed)


def list_documents():
    active = FreeCAD.ActiveDocument
    docs = []
    for name, doc in FreeCAD.listDocuments().items():
        docs.append(
            {
                "name": name,
                "label": doc.Label,
                "file": doc.FileName,
                "objects": len(doc.Objects),
                "active": doc is active,
            }
        )
    return _json(docs)


def list_objects(document=None):
    doc = _document(document)
    objects = []
    for obj in doc.Objects:
        objects.append(
            {
                "name": obj.Name,
                "label": obj.Label,
                "type": obj.TypeId,
                "valid": obj.isValid(),
                "parents": [p.Name for p in obj.InList],
            }
        )
    return _json({"document": doc.Name, "objects": objects})


def get_object(name, document=None):
    doc = _document(document)
    obj = doc.getObject(name)
    if obj is None:
        raise ValueError("No object named '{}' in document '{}'.".format(name, doc.Name))
    properties = {}
    for prop in obj.PropertiesList:
        try:
            value = _short_repr(obj.getPropertyByName(prop))
        except Exception as e:
            value = "<error: {}>".format(e)
        properties[prop] = {"type": obj.getTypeIdOfProperty(prop), "value": value}
    return _json(
        {
            "document": doc.Name,
            "name": obj.Name,
            "label": obj.Label,
            "type": obj.TypeId,
            "properties": properties,
        }
    )


def screenshot(width=800, height=600):
    import FreeCADGui

    gui_doc = FreeCADGui.ActiveDocument
    view = gui_doc.ActiveView if gui_doc else None
    if view is None or not hasattr(view, "saveImage"):
        raise ValueError("There is no active 3D view.")
    fd, path = tempfile.mkstemp(suffix=".png", prefix="freecad-mcp-")
    os.close(fd)
    try:
        view.saveImage(path, int(width), int(height), "Current")
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
    finally:
        os.remove(path)
    return {"content": [{"type": "image", "data": data, "mimeType": "image/png"}]}


def call_tool(name, arguments, client_name=""):
    """Run tool name and return an MCP CallToolResult."""
    arguments = arguments or {}
    if not isinstance(arguments, dict):
        return common.tool_error("Tool arguments must be an object.")
    try:
        if name == "execute_python":
            return execute_python(arguments.get("code"), client_name)
        if name == "list_documents":
            return list_documents()
        if name == "list_objects":
            return list_objects(arguments.get("document"))
        if name == "get_object":
            return get_object(arguments.get("name"), arguments.get("document"))
        if name == "screenshot":
            return screenshot(arguments.get("width", 800), arguments.get("height", 600))
    except Exception as e:
        return common.tool_error("{}: {}".format(type(e).__name__, e))
    return common.tool_error("Unknown tool '{}'.".format(name))
