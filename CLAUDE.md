# RenderDoc MCP Server

作为 RenderDoc UI 扩展运行的 MCP 服务器。支持 AI 助手访问 RenderDoc 的抓帧数据，辅助 DirectX 11/12 图形调试。

## 架构

**混合进程分离方式**：

```
Claude/AI Client (stdio)
        │
        ▼
MCP Server Process (标准 Python + FastMCP 2.0)
        │ TCP Socket (默认端口 19876)
        ▼
RenderDoc Process (Extension + 非阻塞 Socket + QTimer, 监听 0.0.0.0)
```

## 项目结构

```
RenderDocMCP/
├── mcp_server/                        # MCP 服务器
│   ├── server.py                      # FastMCP 入口
│   ├── config.py                      # 配置
│   └── bridge/
│       └── client.py                  # TCP Socket 客户端
│
├── renderdoc_extension/               # RenderDoc 扩展
│   ├── __init__.py                    # register()/unregister()
│   ├── extension.json                 # 清单文件
│   ├── socket_server.py               # TCP Socket 服务端（非阻塞 + QTimer）
│   ├── file_server.py                 # HTTP 文件服务（导出文件下载）
│   ├── request_handler.py             # 请求处理
│   ├── renderdoc_facade.py            # RenderDoc API 封装
│   └── services/
│       ├── export_service.py          # 文件导出（texture→PNG, mesh→OBJ）
│       └── ...
│
└── scripts/
    └── install_extension.py           # 扩展安装脚本
```

## MCP 工具

| 工具名 | 说明 |
|--------|------|
| `list_captures` | 获取指定目录内的 .rdc 文件列表 |
| `open_capture` | 打开抓帧文件（已有抓帧会自动关闭） |
| `get_capture_status` | 检查抓帧加载状态 |
| `get_draw_calls` | Draw Call 列表（层级结构，支持过滤） |
| `get_frame_summary` | 帧整体统计信息（Draw Call 数、Marker 列表等） |
| `find_draws_by_shader` | 按 Shader 名称反向查找 Draw Call |
| `find_draws_by_texture` | 按纹理名称反向查找 Draw Call |
| `find_draws_by_resource` | 按资源 ID 反向查找 Draw Call |
| `get_draw_call_details` | 特定 Draw Call 的详细信息 |
| `get_action_timings` | 获取 Action 的 GPU 执行时间 |
| `get_shader_info` | Shader 源码 / 常量缓冲区 |
| `get_buffer_contents` | 获取缓冲区数据（支持偏移/长度指定） |
| `get_texture_info` | 纹理元数据 |
| `get_texture_data` | 纹理像素数据（支持 mip/slice/3D 切片） |
| `get_pipeline_state` | 完整管线状态 |
| `export_texture` | 导出纹理为 PNG 文件，返回下载 URL（不经过模型） |
| `export_mesh` | 导出 Draw Call 的 Mesh 为 OBJ 文件，返回下载 URL |

### get_draw_calls 过滤选项

```python
get_draw_calls(
    include_children=True,      # 包含子 Action
    marker_filter="Camera.Render",  # 仅获取该 Marker 下的内容
    exclude_markers=["GUI.Repaint", "UIR.DrawChain"],  # 排除的 Marker
    event_id_min=7372,          # event_id 范围起始
    event_id_max=7600,          # event_id 范围结束
    only_actions=True,          # 排除 Marker（仅 Draw Call）
    flags_filter=["Drawcall", "Dispatch"],  # 仅特定标志
)
```

### 抓帧管理工具

```python
# 列举目录内的抓帧文件
list_captures(directory="D:\\captures")
# → {"count": 3, "captures": [{"filename": "game.rdc", "path": "...", "size_bytes": 12345, "modified_time": "..."}, ...]}

# 打开抓帧文件（已有抓帧会自动关闭）
open_capture(capture_path="D:\\captures\\game.rdc")
# → {"success": true, "filename": "game.rdc", "api": "D3D11"}
```

### 反向查找工具

```python
# 按 Shader 名称搜索（部分匹配）
find_draws_by_shader(shader_name="Toon", stage="pixel")

# 按纹理名称搜索（部分匹配）
find_draws_by_texture(texture_name="CharacterSkin")

# 按资源 ID 搜索（精确匹配）
find_draws_by_resource(resource_id="ResourceId::12345")
```

### GPU 计时获取

```python
# 获取所有 Action 的计时
get_action_timings()
# → {"available": true, "unit": "CounterUnit.Seconds", "timings": [...], "total_duration_ms": 12.5, "count": 150}

# 仅获取特定事件 ID 的计时
get_action_timings(event_ids=[100, 200, 300])

# 按 Marker 过滤
get_action_timings(marker_filter="Camera.Render", exclude_markers=["GUI.Repaint"])
```

**注意**：GPU 计时计数器在部分硬件/驱动上可能不可用。
当返回 `available: false` 时，该抓帧无法获取计时信息。

### 文件导出工具

导出工具将文件保存到 RenderDoc 主机本地，通过 HTTP 文件服务器提供下载。
返回值仅包含 URL 和元信息，**大文件数据不经过 AI 模型上下文**。

```python
# 导出纹理为 PNG
export_texture(resource_id="ResourceId::12345", event_id=100, mip=0, slice=0)
# → {"url": "http://192.168.1.100:19877/tex_12345_eid100_mip0.png", "size_bytes": 524288, ...}

# 导出 Mesh 为 OBJ
export_mesh(event_id=100)
# → {"url": "http://192.168.1.100:19877/mesh_eid100.obj", "vertex_count": 1500, "face_count": 3000, ...}
```

## 通信协议

TCP Socket（长度前缀帧协议）：
- RenderDoc 端默认监听：`0.0.0.0:19876`（所有网络接口）
- MCP Server 端默认连接：`127.0.0.1:19876`
- 帧格式：`[4 字节载荷长度 (big-endian)] + [JSON 载荷 (UTF-8)]`
- RenderDoc 端：非阻塞 Socket + QTimer（10ms 轮询）
- MCP Server 端：标准 socket 模块 + 懒连接 + 自动重连
- 环境变量：通过 `RENDERDOC_MCP_HOST`、`RENDERDOC_MCP_PORT` 配置

### HTTP 文件服务（导出下载）

- RenderDoc 端默认监听：`0.0.0.0:19877`（daemon 线程）
- 仅提供导出目录内的静态文件下载
- 环境变量配置：

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `RENDERDOC_MCP_FILE_SERVER_PORT` | `19877` | HTTP 文件服务端口 |
| `RENDERDOC_MCP_EXPORT_DIR` | `%TEMP%\renderdoc_mcp_exports` | 导出文件存储目录 |
| `RENDERDOC_MCP_EXPORT_RETENTION_DAYS` | `7` | 文件保留天数（0=不自动清理） |
| `RENDERDOC_MCP_EXTERNAL_HOST` | 自动检测 LAN IP | 导出 URL 中使用的主机地址（覆盖自动检测） |

URL 主机地址解析优先级：`RENDERDOC_MCP_EXTERNAL_HOST` > 绑定地址（非 0.0.0.0 时直接使用）> UDP 探测 LAN IP > `socket.gethostbyname()` > `127.0.0.1`

## 开发笔记

- RenderDoc 内置 Python 默认不包含 `_socket.pyd`，需从标准 CPython 手动补充（详见 `docs/tcp-migration-plan.md`）
- RenderDoc 扩展仅使用 Python 3.6 标准库（不可使用 f-string）
- 对 ReplayController 的访问需通过 `BlockInvoke` 进行

## 参考链接

- [FastMCP](https://github.com/jlowin/fastmcp)
- [RenderDoc Python API](https://renderdoc.org/docs/python_api/index.html)
- [RenderDoc Extension Registration](https://renderdoc.org/docs/how/how_python_extension.html)
