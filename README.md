# GEF MCP Server

GDB Enhanced Features (GEF) 的 Model Context Protocol (MCP) 服务器实现，支持通过 SSE (Server-Sent Events) 进行连接。

## 功能特性

- **GEF/GDB 集成**: 自动加载 GEF 插件，提供增强的调试功能
- **多会话支持**: 最多支持 4 个并发调试会话
- **完整命令集**: 支持所有 GEF 和 GDB 原生命令
- **SSE 传输**: 通过 HTTP SSE 进行实时通信
- **PTY 终端**: 模拟真实终端环境，支持交互式调试

## 安装

### 1. 安装依赖

```bash
cd /root/Desktop/gef-mcp
pip install -r requirements.txt
```

### 2. 安装 GEF (如果尚未安装)

```bash
# 方法1: 使用官方安装脚本
bash -c "$(curl -fsSL https://gef.blah.cat/sh)"

# 方法2: 手动安装
wget -O ~/.gdbinit-gef.py https://raw.githubusercontent.com/hugsy/gef/main/gef.py
echo "source ~/.gdbinit-gef.py" >> ~/.gdbinit
```

## 使用方法

### 启动服务器

```bash
# 默认 SSE 模式，监听 0.0.0.0:8000
python server.py

# 指定主机和端口
python server.py --host 127.0.0.1 --port 8080

# 使用 stdio 模式
python server.py --transport stdio
```

### MCP 端点

- **SSE 端点**: `http://localhost:8000/sse`
- **消息端点**: `http://localhost:8000/messages/`

## 可用工具

### 会话管理

| 工具名 | 描述 |
|--------|------|
| `create_session` | 创建新的 GEF 调试会话 |
| `list_sessions` | 列出所有活跃的会话 |
| `close_session` | 关闭指定会话 |

### 文件和调试

| 工具名 | 描述 |
|--------|------|
| `load_file` | 加载目标二进制文件 |
| `start_debugging` | 使用 `gef 文件名` 方式启动调试 (Kali新版) |
| `set_breakpoint` | 设置断点 |
| `run` | 运行程序 |
| `continue` | 继续执行 |
| `step` | 单步执行 (进入函数) |
| `next` | 单步执行 (跳过函数) |

### 内存和寄存器

| 工具名 | 描述 |
|--------|------|
| `get_registers` | 获取寄存器状态 |
| `examine_memory` | 检查内存内容 |
| `disassemble` | 反汇编代码 |
| `get_backtrace` | 获取调用栈 |

### GEF 增强命令

| 工具名 | 描述 |
|--------|------|
| `vmmap` | 显示虚拟内存映射 |
| `heap` | 显示堆信息 |
| `telescope` | 递归解引用内存 (望远镜命令) |
| `search_pattern` | 在内存中搜索模式 |

### 通用命令

| 工具名 | 描述 |
|--------|------|
| `execute_command` | 执行任意 GEF/GDB 命令 |

## 使用示例

### 基本调试流程

```python
# 1. 创建会话
result = await call_tool("create_session", {})
session_id = result["session_id"]

# 2. 加载目标文件 (方式1: 使用 load_file)
await call_tool("load_file", {
    "session_id": session_id,
    "filepath": "/path/to/binary"
})

# 2. 加载目标文件 (方式2: 使用 start_debugging - 类似 gef 文件名)
await call_tool("start_debugging", {
    "session_id": session_id,
    "filepath": "/path/to/binary"
})

# 3. 设置断点
await call_tool("set_breakpoint", {
    "session_id": session_id,
    "location": "main"
})

# 4. 运行程序
await call_tool("run", {
    "session_id": session_id,
    "args": "--help"
})

# 5. 获取寄存器状态
await call_tool("get_registers", {
    "session_id": session_id
})

# 6. 检查内存
await call_tool("examine_memory", {
    "session_id": session_id,
    "address": "$rsp",
    "count": 16
})

# 7. 使用 GEF 的 telescope 命令
await call_tool("telescope", {
    "session_id": session_id,
    "address": "$rsp",
    "count": 10
})

# 8. 关闭会话
await call_tool("close_session", {
    "session_id": session_id
})
```

### 执行任意 GEF 命令

```python
# 查看所有函数
await call_tool("execute_command", {
    "session_id": session_id,
    "command": "info functions"
})

# 查看符号表
await call_tool("execute_command", {
    "session_id": session_id,
    "command": "info variables"
})

# 设置环境变量后运行
await call_tool("execute_command", {
    "session_id": session_id,
    "command": "set environment LD_PRELOAD=/path/to/lib.so"
})
```

## 配置说明

### GEF 配置

服务器会自动配置以下 GEF 选项：

```bash
gef config context.clear_screen False
gef config context.layout "regs stack code source"
set pagination off
set confirm off
```

### 环境变量

- `GEF_MCP_HOST`: 服务器主机地址 (默认: 0.0.0.0)
- `GEF_MCP_PORT`: 服务器端口 (默认: 8000)

## 与 Claude Desktop 集成

在 `claude_desktop_config.json` 中添加：

```json
{
  "mcpServers": {
    "gef": {
      "command": "python",
      "args": ["/root/Desktop/gef-mcp/server.py", "--transport", "stdio"]
    }
  }
}
```

或者使用 SSE 模式：

```json
{
  "mcpServers": {
    "gef": {
      "url": "http://localhost:8000/sse"
    }
  }
}
```

## 故障排除

### GEF 未加载

如果 GEF 命令不可用，请检查：

1. GEF 是否正确安装: `ls ~/.gdbinit-gef.py`
2. GDB 版本是否支持 Python: `gdb -batch -ex "python print('OK')"`

### 会话超时

长时间运行的命令可能会超时，可以通过 `create_session` 的 `timeout` 参数调整超时时间。

### 权限问题

调试某些程序可能需要 root 权限，请确保服务器以适当的权限运行。

## 许可证

MIT License

## 参考

- [GEF 官方文档](https://hugsy.github.io/gef/)
- [MCP 协议规范](https://modelcontextprotocol.io/)
