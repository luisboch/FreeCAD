# SPDX-License-Identifier: LGPL-2.1-or-later

SET(MCP_SRCS
    Init.py
    InitGui.py
    TestMCPApp.py
)

SET(MCP_Package_SRCS
    fc_mcp/__init__.py
    fc_mcp/auth.py
    fc_mcp/bridge.py
    fc_mcp/clientconfig.py
    fc_mcp/common.py
    fc_mcp/preferences.py
    fc_mcp/server.py
    fc_mcp/tools.py
)

SOURCE_GROUP("" FILES ${MCP_SRCS})
SOURCE_GROUP("fc_mcp" FILES ${MCP_Package_SRCS})

ADD_CUSTOM_TARGET(MCP ALL
    SOURCES ${MCP_SRCS} ${MCP_Package_SRCS}
)

fc_copy_sources(MCP "${CMAKE_BINARY_DIR}/Mod/MCP" ${MCP_SRCS} ${MCP_Package_SRCS})

INSTALL(
    FILES
        ${MCP_SRCS}
    DESTINATION
        Mod/MCP
)

INSTALL(
    FILES
        ${MCP_Package_SRCS}
    DESTINATION
        Mod/MCP/fc_mcp
)
