"""
RenderDoc MCP Server
Official MCP SDK server providing access to RenderDoc capture data.
"""

import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .bridge.client import RenderDocBridge, RenderDocBridgeError
from .config import settings

server = Server("RenderDoc MCP Server")
bridge = RenderDocBridge(host=settings.renderdoc_host, port=settings.renderdoc_port)

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="get_capture_status",
        description="Check if a capture is currently loaded in RenderDoc. Returns the capture status and API type if loaded.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_draw_calls",
        description=(
            "Get the list of all draw calls and actions in the current capture. "
            "Returns a hierarchical tree of actions including markers, draw calls, dispatches, and other GPU events."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "include_children": {"type": "boolean", "description": "Include child actions in the hierarchy (default: true)", "default": True},
                "marker_filter": {"type": "string", "description": "Only include actions under markers containing this string (partial match)"},
                "exclude_markers": {"type": "array", "items": {"type": "string"}, "description": "Exclude actions under markers containing these strings"},
                "event_id_min": {"type": "integer", "description": "Only include actions with event_id >= this value"},
                "event_id_max": {"type": "integer", "description": "Only include actions with event_id <= this value"},
                "only_actions": {"type": "boolean", "description": "If true, exclude marker actions (PushMarker/PopMarker/SetMarker)", "default": False},
                "flags_filter": {"type": "array", "items": {"type": "string"}, "description": 'Only include actions with these flags (e.g. ["Drawcall", "Dispatch"])'},
            },
        },
    ),
    Tool(
        name="get_frame_summary",
        description=(
            "Get a summary of the current capture frame. Returns statistics about the frame including: "
            "API type, total action count, statistics (draw calls, dispatches, clears, copies, presents, markers), "
            "top-level markers with event IDs and child counts, and resource counts (textures, buffers)."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="find_draws_by_shader",
        description="Find all draw calls using a shader with the given name (partial match). Returns a list of matching draw calls with event IDs and match reasons.",
        inputSchema={
            "type": "object",
            "properties": {
                "shader_name": {"type": "string", "description": "Partial name to search for in shader names or entry points"},
                "stage": {"type": "string", "enum": ["vertex", "hull", "domain", "geometry", "pixel", "compute"], "description": "Optional shader stage to search (if not specified, searches all stages)"},
            },
            "required": ["shader_name"],
        },
    ),
    Tool(
        name="find_draws_by_texture",
        description="Find all draw calls using a texture with the given name (partial match). Returns a list of matching draw calls with event IDs and match reasons. Searches SRVs, UAVs, and render targets.",
        inputSchema={
            "type": "object",
            "properties": {
                "texture_name": {"type": "string", "description": "Partial name to search for in texture resource names"},
            },
            "required": ["texture_name"],
        },
    ),
    Tool(
        name="find_draws_by_resource",
        description='Find all draw calls using a specific resource ID (exact match). Returns a list of matching draw calls with event IDs and match reasons. Searches shaders, SRVs, UAVs, render targets, and depth targets.',
        inputSchema={
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "description": 'Resource ID to search for (e.g. "ResourceId::12345" or "12345")'},
            },
            "required": ["resource_id"],
        },
    ),
    Tool(
        name="get_draw_call_details",
        description="Get detailed information about a specific draw call. Includes vertex/index counts, resource outputs, and other metadata.",
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "The event ID of the draw call to inspect"},
            },
            "required": ["event_id"],
        },
    ),
    Tool(
        name="get_action_timings",
        description=(
            "Get GPU timing information for actions (draw calls, dispatches, etc.). "
            "Returns timing data including: available, unit, timings list, total_duration_ms, count. "
            "Note: GPU timing counters may not be available on all hardware/drivers."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_ids": {"type": "array", "items": {"type": "integer"}, "description": "Optional list of specific event IDs to get timings for. If not specified, returns timings for all actions."},
                "marker_filter": {"type": "string", "description": "Only include actions under markers containing this string (partial match)"},
                "exclude_markers": {"type": "array", "items": {"type": "string"}, "description": "Exclude actions under markers containing these strings"},
            },
        },
    ),
    Tool(
        name="get_shader_info",
        description=(
            "Get shader information for a specific stage at a given event. "
            "Returns shader disassembly, constant buffer values, and resource bindings."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "The event ID to inspect the shader at"},
                "stage": {"type": "string", "enum": ["vertex", "hull", "domain", "geometry", "pixel", "compute"], "description": "The shader stage"},
                "disassembly_target": {"type": "string", "description": 'Disassembly format (substring match, case-insensitive). Defaults to GLSL > HLSL > first. Common: "GLSL", "SPIR-V", "HLSL", "DXBC", "DXIL".'},
            },
            "required": ["event_id", "stage"],
        },
    ),
    Tool(
        name="get_buffer_contents",
        description="Read the contents of a buffer resource. Returns buffer data as base64-encoded bytes along with metadata.",
        inputSchema={
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "description": "The resource ID of the buffer to read"},
                "offset": {"type": "integer", "description": "Byte offset to start reading from (default: 0)", "default": 0},
                "length": {"type": "integer", "description": "Number of bytes to read, 0 for entire buffer (default: 0)", "default": 0},
            },
            "required": ["resource_id"],
        },
    ),
    Tool(
        name="get_texture_info",
        description="Get metadata about a texture resource. Includes dimensions, format, mip levels, and other properties.",
        inputSchema={
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "description": "The resource ID of the texture"},
            },
            "required": ["resource_id"],
        },
    ),
    Tool(
        name="get_texture_data",
        description="Read the pixel data of a texture resource. Returns texture pixel data as base64-encoded bytes along with metadata.",
        inputSchema={
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "description": "The resource ID of the texture to read"},
                "mip": {"type": "integer", "description": "Mip level to retrieve (default: 0)", "default": 0},
                "slice": {"type": "integer", "description": "Array slice or cube face index (default: 0). For cube maps: 0=X+, 1=X-, 2=Y+, 3=Y-, 4=Z+, 5=Z-", "default": 0},
                "sample": {"type": "integer", "description": "MSAA sample index (default: 0)", "default": 0},
                "depth_slice": {"type": "integer", "description": "For 3D textures only, extract a specific depth slice"},
            },
            "required": ["resource_id"],
        },
    ),
    Tool(
        name="get_pipeline_state",
        description=(
            "Get the full graphics pipeline state at a specific event. Returns detailed pipeline state including: "
            "bound shaders, shader resources (SRVs), UAVs, samplers, constant buffers, render targets, depth target, viewports, and input assembly state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "The event ID to get pipeline state at"},
            },
            "required": ["event_id"],
        },
    ),
    Tool(
        name="get_cbuffer_values",
        description="Get the actual values of one constant buffer bound at a specific event and stage.",
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "The event ID to inspect"},
                "stage": {"type": "string", "enum": ["vertex", "pixel", "compute"], "description": "Shader stage"},
                "cbuffer_name": {"type": "string", "description": "Constant buffer name to resolve (takes priority over index)"},
                "cbuffer_index": {"type": "integer", "description": "Constant buffer index in ShaderReflection.constantBlocks"},
                "include_raw_bytes": {"type": "boolean", "description": "Also return base64 raw bytes for the bound range", "default": False},
            },
            "required": ["event_id", "stage"],
        },
    ),
    Tool(
        name="export_texture",
        description=(
            "Export a texture to PNG file and return a download URL. "
            "The texture is saved to the export directory on the RenderDoc host and served via HTTP. "
            "Render targets are automatically exported right-side-up."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "description": "The resource ID of the texture to export"},
                "event_id": {"type": "integer", "description": "The event ID at which to capture the texture state"},
                "mip": {"type": "integer", "description": "Mip level to export (default: 0)", "default": 0},
                "slice": {"type": "integer", "description": "Array slice or cube face index (default: 0)", "default": 0},
                "flip_y": {"type": "boolean", "description": "Override vertical flip. Default: auto-detect based on API and render target status"},
            },
            "required": ["resource_id", "event_id"],
        },
    ),
    Tool(
        name="export_shader",
        description=(
            "Export the bound shader disassembly at a draw/dispatch event and return a download URL. "
            "The shader disassembly is written to a text file served via HTTP."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "The event ID at which to inspect the pipeline"},
                "stage": {"type": "string", "enum": ["vertex", "hull", "domain", "geometry", "pixel", "compute"], "description": "Shader stage to export"},
                "disassembly_target": {"type": "string", "description": 'Disassembly format (substring match, case-insensitive). Common: "GLSL", "SPIR-V", "HLSL", "DXBC", "DXIL".'},
            },
            "required": ["event_id", "stage"],
        },
    ),
    Tool(
        name="export_mesh",
        description=(
            "Export the mesh geometry at a draw call to OBJ file and return a download URL. "
            "Extracts vertex positions, normals, and texture coordinates. "
            "Coordinate system and UV conventions are automatically matched to the OBJ format."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "The event ID of the draw call whose mesh to export"},
                "flip_uv_v": {"type": "boolean", "description": "Override UV V-coordinate flip. Default: auto-detect based on graphics API"},
                "flip_handedness": {"type": "boolean", "description": "Override coordinate system handedness conversion. Default: auto-detect based on graphics API"},
            },
            "required": ["event_id"],
        },
    ),
    Tool(
        name="list_captures",
        description="List all RenderDoc capture files (.rdc) in the specified directory.",
        inputSchema={
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "The directory path to search for capture files"},
            },
            "required": ["directory"],
        },
    ),
    Tool(
        name="open_capture",
        description="Open a RenderDoc capture file (.rdc). This will close any currently open capture.",
        inputSchema={
            "type": "object",
            "properties": {
                "capture_path": {"type": "string", "description": "Full path to the capture file to open"},
            },
            "required": ["capture_path"],
        },
    ),
]

_TOOL_MAP = {t.name: t for t in TOOLS}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name not in _TOOL_MAP:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    # Validate cbuffer_values requires name or index
    if name == "get_cbuffer_values":
        if arguments.get("cbuffer_name") is None and arguments.get("cbuffer_index") is None:
            return [TextContent(type="text", text="Either cbuffer_name or cbuffer_index is required")]

    # Build params dict, stripping None values
    params = {k: v for k, v in arguments.items() if v is not None}

    try:
        result = await asyncio.to_thread(bridge.call, name, params if params else None)
    except RenderDocBridgeError as e:
        return [TextContent(type="text", text=f"RenderDoc error: {e}")]

    import json
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    """Run the MCP server"""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
