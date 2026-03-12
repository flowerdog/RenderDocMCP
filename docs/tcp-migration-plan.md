# RenderDoc MCP Bridge: 文件 IPC → TCP Socket 改造方案

## 背景

原始实现采用文件 IPC（通过 `%TEMP%/renderdoc_mcp/` 目录下的 JSON 文件通信），原因是 RenderDoc 内置 Python 缺少 `_socket` C 扩展模块。

通过将标准 Python 的 `_socket.pyd`（及相关依赖）补充到 RenderDoc 的 Python 环境中，`socket` 模块现已可用。本方案将通信方式改造为 TCP Socket，以获得：

- 更低的延迟（~10ms 轮询 vs ~150ms 文件轮询）
- 支持跨机器远程调试
- 消除文件系统竞态条件
- 更明确的连接状态管理

## 前置条件：补充 socket 模块

RenderDoc 官方发布版的内置 Python 不包含 `_socket.pyd`。每次 RenderDoc 升级后需要重新补充：

1. 确认 RenderDoc 内置 Python 版本：在 RenderDoc Python Shell 中执行 `import sys; print(sys.version)`
2. 从对应版本的标准 CPython 安装目录 `Python3x/DLLs/` 中复制以下文件到 RenderDoc 的 Python DLLs 目录：
   - `_socket.pyd`
   - 如有 SSL 需求还需：`_ssl.pyd`、`select.pyd`
3. 验证：在 RenderDoc Python Shell 中执行 `import socket; print("OK")`

## 架构

### 改造前（文件 IPC）

```
MCP Server Process
 │
 │  写 request.json / 轮询 response.json (50ms)
 ▼
%TEMP%/renderdoc_mcp/
 │
 │  QTimer 轮询 request.json (100ms)
 ▼
RenderDoc Process (Extension)
```

### 改造后（TCP Socket）

```
MCP Server Process (标准 Python)
 │
 │  TCP 连接 (host:port, 默认 127.0.0.1:19876)
 │  长度前缀帧协议
 ▼
RenderDoc Process (Extension, 非阻塞 socket + QTimer 10ms 轮询)
```

## 帧协议

```
┌──────────────────┬──────────────────────────┐
│ 4 bytes          │ N bytes                  │
│ payload length   │ JSON (UTF-8)             │
│ (big-endian u32) │                          │
└──────────────────┴──────────────────────────┘
```

请求/响应 JSON 格式与原文件 IPC 完全一致，`RequestHandler` 无需修改。

### 请求示例

```json
{"id": "uuid", "method": "get_draw_calls", "params": {"include_children": true}}
```

### 响应示例

```json
{"id": "uuid", "result": {"actions": [...]}}
```

## 修改文件清单

### 1. `renderdoc_extension/socket_server.py` — 重写

- 原实现：文件轮询 IPC 服务端
- 新实现：非阻塞 TCP 服务端
- 使用 `socket` 模块创建非阻塞 TCP server
- 保留 `QTimer` 轮询（10ms 间隔）驱动 accept/recv
- 实现长度前缀帧的收发
- 保持 `MCPBridgeServer` 类名和 `start()/stop()/is_running()` 接口不变
- **约束**：RenderDoc Python 3.6 语法（无 f-string、无 walrus operator）

### 2. `mcp_server/bridge/client.py` — 重写

- 原实现：文件 IPC 客户端
- 新实现：TCP Socket 客户端
- 使用标准 `socket` 模块
- 懒连接 + 断线自动重连
- 移除所有文件 IPC 代码

### 3. `renderdoc_extension/__init__.py` — 小幅更新

- 更新状态对话框文案

### 4. `CLAUDE.md` — 文档更新

- 通信协议描述从文件 IPC 改为 TCP Socket

### 不修改的文件

- `mcp_server/server.py` — `bridge.call()` 接口不变
- `mcp_server/config.py` — 已有 host/port 配置
- `renderdoc_extension/request_handler.py` — 请求格式不变
- `renderdoc_extension/renderdoc_facade.py` — 不涉及通信
- `renderdoc_extension/services/*` — 不涉及通信
- `renderdoc_extension/utils/*` — 不涉及通信

## RenderDoc 内置 Python 环境约束

| 模块 | 可用性 | 备注 |
|------|--------|------|
| `socket` | 需手动补充 `_socket.pyd` | 补充后可用 |
| `PySide2.QtCore` | 可用 | QObject、QTimer |
| `PySide2.QtNetwork` | 不可用 | — |
| `ctypes` | 可用 | 备用方案（直接调用 ws2_32.dll） |
| `json` | 可用 | 标准库 |
| `struct` | 可用 | 标准库 |
| `threading` | 未验证 | 即使可用也不推荐（Qt 线程安全问题） |

## 配置

通过环境变量控制：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `RENDERDOC_MCP_HOST` | `127.0.0.1` | RenderDoc TCP 服务端地址 |
| `RENDERDOC_MCP_PORT` | `19876` | RenderDoc TCP 服务端端口 |

## 故障排查

| 症状 | 可能原因 | 解决方法 |
|------|----------|----------|
| MCP 连接超时 | RenderDoc 未启动或扩展未加载 | 检查 RenderDoc Python Shell 输出 |
| `No module named '_socket'` | RenderDoc 升级后 `_socket.pyd` 丢失 | 重新补充对应版本的 `_socket.pyd` |
| 连接被拒绝 | 端口冲突或防火墙 | 检查端口占用，更换端口 |
| 远程连接失败 | 监听地址为 127.0.0.1 | 改为 `0.0.0.0`（注意安全性） |
