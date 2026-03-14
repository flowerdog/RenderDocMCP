# RenderDoc MCP Server

作为 RenderDoc UI 扩展运行的 MCP 服务器。支持 AI 助手访问 RenderDoc 的抓帧数据，辅助图形调试。

## 架构

```
Claude/AI Client (stdio)
        │
        ▼
MCP Server Process (Python + FastMCP 2.0)
        │ TCP Socket (默认端口 19876)
        ▼
RenderDoc Process (Extension, 监听 0.0.0.0)
        │
        └── HTTP File Server (端口 19877, 导出文件下载)
```

RenderDoc 内置 Python 默认不包含 `_socket.pyd`，需从标准 CPython 手动补充后方可使用 TCP 通信（详见 `docs/tcp-migration-plan.md`）。

## 安装配置

### 1. 安装 RenderDoc 扩展

```bash
python scripts/install_extension.py
```

扩展将安装到 `%APPDATA%\qrenderdoc\extensions\renderdoc_mcp_bridge`。

### 2. 在 RenderDoc 中启用扩展

1. 启动 RenderDoc
2. Tools > Manage Extensions
3. 启用 "RenderDoc MCP Bridge"

### 3. 安装 MCP 服务器

```bash
uv tool install
uv tool update-shell  # 添加到 PATH
```

重启 Shell 后即可使用 `renderdoc-mcp` 命令。

> **注意**：加上 `--editable` 参数后，源码修改会立即生效（开发时推荐）。
> 安装稳定版请使用 `uv tool install .`。

### 4. 配置 MCP 客户端

#### Claude Desktop

在 `claude_desktop_config.json` 中添加：

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "renderdoc-mcp",
      "env": {
        "RENDERDOC_MCP_HOST": "192.168.1.100",
        "RENDERDOC_MCP_PORT": "19876"
      }
    }
  }
}
```

#### Claude Code

在 `.mcp.json` 中添加：

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "renderdoc-mcp",
      "env": {
        "RENDERDOC_MCP_HOST": "192.168.1.100",
        "RENDERDOC_MCP_PORT": "19876"
      }
    }
  }
}
```

将 `RENDERDOC_MCP_HOST` 替换为 RenderDoc 所在机器的 IP（例如同局域网调试机 IP）。
`RENDERDOC_MCP_PORT` 默认为 `19876`，仅在你修改过扩展监听端口时才需要同步调整。

## 使用方法

1. 启动 RenderDoc，打开抓帧文件 (.rdc)
2. 通过 MCP 客户端（如 Claude）访问 RenderDoc 数据

## MCP 工具列表

| 工具 | 说明 |
|------|------|
| `get_capture_status` | 检查抓帧加载状态 |
| `get_draw_calls` | 获取 Draw Call 层级列表 |
| `get_draw_call_details` | 获取特定 Draw Call 的详细信息 |
| `get_shader_info` | 获取 Shader 源码和常量缓冲区值 |
| `get_buffer_contents` | 获取缓冲区内容 (Base64) |
| `get_texture_info` | 获取纹理元数据 |
| `get_texture_data` | 获取纹理像素数据 (Base64) |
| `get_pipeline_state` | 获取管线状态 |
| `export_texture` | 导出纹理为 PNG，返回下载 URL |
| `export_shader` | 导出 Shader 反汇编为 TXT，返回下载 URL |
| `export_mesh` | 导出 Mesh 为 OBJ，返回下载 URL |

## 使用示例

### 获取 Draw Call 列表

```
get_draw_calls(include_children=true)
```

### 获取 Shader 信息

```
get_shader_info(event_id=123, stage="pixel")
```

### 获取管线状态

```
get_pipeline_state(event_id=123)
```

### 获取纹理数据

```
# 获取 2D 纹理的 mip 0
get_texture_data(resource_id="ResourceId::123")

# 获取特定 mip 级别
get_texture_data(resource_id="ResourceId::123", mip=2)

# 获取 CubeMap 的特定面 (0=X+, 1=X-, 2=Y+, 3=Y-, 4=Z+, 5=Z-)
get_texture_data(resource_id="ResourceId::456", slice=3)

# 获取 3D 纹理的特定深度切片
get_texture_data(resource_id="ResourceId::789", depth_slice=5)
```

### 部分获取缓冲区数据

```
# 获取整个缓冲区
get_buffer_contents(resource_id="ResourceId::123")

# 从偏移 256 处获取 512 字节
get_buffer_contents(resource_id="ResourceId::123", offset=256, length=512)
```

### 导出文件（不经过模型上下文）

导出工具将文件保存到 RenderDoc 主机本地，通过内置 HTTP 服务器提供下载 URL。
大文件数据不会进入 AI 模型上下文，仅返回 URL 和元信息。

```
# 导出纹理为 PNG
export_texture(resource_id="ResourceId::123", event_id=100)
# → {"url": "http://host:19877/tex_123_eid100_mip0.png", "size_bytes": 524288, ...}

# 导出 Pixel Shader 反汇编为 TXT
export_shader(event_id=100, stage="pixel")
# → {"url": "http://host:19877/shader_pixel_eid100.txt", "size_bytes": 16384, ...}

# 导出 Mesh 为 OBJ
export_mesh(event_id=100)
# → {"url": "http://host:19877/mesh_eid100.obj", "vertex_count": 1500, ...}
```

### 文件服务器配置

导出文件通过 HTTP 文件服务器提供下载，可通过环境变量配置：

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `RENDERDOC_MCP_FILE_SERVER_PORT` | `19877` | HTTP 文件服务端口 |
| `RENDERDOC_MCP_EXPORT_DIR` | `%TEMP%\renderdoc_mcp_exports` | 导出文件存储目录 |
| `RENDERDOC_MCP_EXPORT_RETENTION_DAYS` | `7` | 文件保留天数（0=不自动清理） |
| `RENDERDOC_MCP_EXTERNAL_HOST` | 自动检测 LAN IP | 导出 URL 中使用的主机地址 |

URL 中的主机地址默认自动检测本机 LAN IP。如果自动检测不准确，可通过 `RENDERDOC_MCP_EXTERNAL_HOST` 显式指定。
在 RenderDoc 启动前设置环境变量即可生效。

## 系统要求

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- RenderDoc 1.20+

> **注意**：仅在 Windows + DirectX 11 环境下验证过。
> Linux/macOS + Vulkan/OpenGL 环境可能也能运行，但尚未测试。

## 许可证

MIT
