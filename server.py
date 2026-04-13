#!/usr/bin/env python3
"""
GEF MCP Server - GDB Enhanced Features Model Context Protocol Server
为Kali Linux最新版GEF提供MCP SSE接口
"""

import os
import sys
import pty
import select
import subprocess
import uuid
import threading
import time
import signal
import re
import json
from typing import Dict, Optional, Any, List
from contextlib import contextmanager
from dataclasses import dataclass, asdict

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
    from mcp.server.sse import SseServerTransport
except ImportError:
    print("Error: mcp library not installed. Run: pip install mcp")
    sys.exit(1)


ANSI_ESCAPE_PATTERN = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
ANSI_ESCAPE_EXTENDED = re.compile(r'\x1b\][^\x07]*\x07')
ANSI_ESCAPE_OTHER = re.compile(r'\x1b\[^[A-Z@]')
CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def clean_output(output: str) -> str:
    """清理GDB/GEF输出中的ANSI转义序列和控制字符"""
    if not output:
        return ""
    cleaned = ANSI_ESCAPE_PATTERN.sub('', output)
    cleaned = ANSI_ESCAPE_EXTENDED.sub('', cleaned)
    cleaned = ANSI_ESCAPE_OTHER.sub('', cleaned)
    cleaned = CONTROL_CHARS.sub('', cleaned)
    cleaned = cleaned.replace('\r\n', '\n').replace('\r', '\n')
    lines = [line.rstrip() for line in cleaned.split('\n')]
    while lines and not lines[-1]:
        lines.pop()
    return '\n'.join(lines)


@dataclass
class BreakpointInfo:
    """断点信息"""
    number: int
    type: str
    disp: str
    enabled: str
    address: str
    what: str


@dataclass
class MemoryMapping:
    """内存映射信息"""
    start: str
    end: str
    size: str
    offset: str
    permissions: str
    pathname: str


class GefSession:
    """GEF/GDB会话管理"""
    
    def __init__(self, session_id: str, timeout: int = 10):
        self.session_id = session_id
        self.timeout = timeout
        self.master_fd: Optional[int] = None
        self.child_pid: Optional[int] = None
        self.ps1_marker = f"GEF_MCP_END_{session_id}"
        self._lock = threading.Lock()
        self._closed = False
        self.target_file: Optional[str] = None
        self._init_pty()
    
    def _init_pty(self):
        """初始化PTY会话"""
        env = os.environ.copy()
        env['PS1'] = f'\\[{self.ps1_marker}\\]\\$ '
        env['TERM'] = 'xterm-256color'
        
        self.child_pid, self.master_fd = pty.fork()
        
        if self.child_pid == 0:
            # 子进程 - 启动GEF (Kali Linux新版本)
            gef_path = self._find_gef()
            if gef_path:
                os.execv(gef_path, [gef_path, '-q'])
            else:
                # 回退到GDB手动加载GEF
                gdb_path = self._find_gdb()
                os.execv(gdb_path, [gdb_path, '-q', '--nh'])
        else:
            # 父进程
            time.sleep(0.5)
            self._setup_gef()
    
    def _find_gdb(self) -> str:
        """查找GDB路径"""
        for path in ['/usr/bin/gdb', '/usr/local/bin/gdb', 'gdb']:
            if os.path.exists(path) or path == 'gdb':
                return path
        return 'gdb'
    
    def _find_gef(self) -> Optional[str]:
        """查找GEF命令路径 (Kali Linux新版本)"""
        for path in ['/usr/bin/gef', '/usr/local/bin/gef']:
            if os.path.exists(path):
                return path
        return None
    
    def _setup_gef(self):
        """设置GEF环境"""
        # 加载GEF
        gef_paths = [
            '/tmp/gef.py',
            os.path.expanduser('~/.gdbinit-gef.py'),
            os.path.expanduser('~/.gef.py'),
            '/usr/share/gef/gef.py',
            '/opt/gef/gef.py',
        ]
        
        gef_loaded = False
        for gef_path in gef_paths:
            if os.path.exists(gef_path):
                self._send_command(f'source {gef_path}')
                gef_loaded = True
                break
        
        if not gef_loaded:
            # 尝试在线加载GEF
            self._send_command('source ~/.gdbinit-gef.py 2>/dev/null || echo "GEF not found"')
        
        # 设置GEF配置
        self._send_command('gef config context.clear_screen False')
        self._send_command('gef config context.layout "regs stack code source"')
        self._send_command('set pagination off')
        self._send_command('set confirm off')
        time.sleep(0.2)
    
    def _send_command(self, command: str):
        """发送命令到GDB"""
        if self.master_fd and not self._closed:
            try:
                os.write(self.master_fd, f'{command}\n'.encode())
                time.sleep(0.05)
            except OSError:
                pass
    
    def _read_available(self, timeout: float = 0.5) -> str:
        """读取可用输出"""
        output = []
        end_time = time.time() + timeout
        
        while time.time() < end_time:
            remaining = end_time - time.time()
            if remaining <= 0:
                break
            
            ready, _, _ = select.select([self.master_fd], [], [], min(0.1, remaining))
            
            if ready:
                try:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        output.append(data.decode('utf-8', errors='replace'))
                    else:
                        break
                except OSError:
                    break
            else:
                break
        
        return ''.join(output)
    
    def _read_until_prompt(self, timeout: float = None) -> str:
        """读取直到GEF提示符"""
        if timeout is None:
            timeout = self.timeout
        
        output = []
        end_time = time.time() + timeout
        gef_prompt_found = False
        
        while time.time() < end_time:
            remaining = end_time - time.time()
            if remaining <= 0:
                break
            
            ready, _, _ = select.select([self.master_fd], [], [], min(0.1, remaining))
            
            if ready:
                try:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        text = data.decode('utf-8', errors='replace')
                        output.append(text)
                        # 检测GEF提示符 (gef➤ 或 gdb)
                        if 'gef➤' in text or '(gdb)' in text or self.ps1_marker in text:
                            gef_prompt_found = True
                            break
                    else:
                        break
                except OSError:
                    break
        
        result = ''.join(output)
        if not gef_prompt_found:
            result += f"\n[Warning: Timeout waiting for GEF prompt]\n"
        
        return result
    
    def execute_gef_command(self, command: str, extended_timeout: bool = False) -> str:
        """执行GEF/GDB命令
        
        Args:
            command: 要执行的命令
            extended_timeout: 是否使用扩展超时（用于run/continue等长时间运行的命令）
        """
        with self._lock:
            if self._closed:
                return "Error: Session has been closed"
            
            if not command.strip():
                return ""
            
            try:
                # 发送命令
                os.write(self.master_fd, f'{command}\n'.encode())
                time.sleep(0.1)
                
                # 读取输出 - 对于run/continue等命令使用更长的超时
                if extended_timeout:
                    output = self._read_until_prompt(timeout=self.timeout * 3)
                else:
                    output = self._read_until_prompt()
                
                return clean_output(output)
                
            except OSError as e:
                return f"Error executing command: {str(e)}"
            except Exception as e:
                return f"Error: {str(e)}"
    
    def load_file(self, filepath: str) -> str:
        """加载目标文件"""
        if not os.path.exists(filepath):
            return f"Error: File not found: {filepath}"
        
        self.target_file = filepath
        result = self.execute_gef_command(f'file {filepath}')
        return result
    
    def start_debugging(self, filepath: str) -> str:
        """使用 gef 文件名 的方式启动调试 (Kali Linux新版本)"""
        if not os.path.exists(filepath):
            return f"Error: File not found: {filepath}"
        
        self.target_file = filepath
        # 使用 gef 命令重新加载文件，这会以 gef 文件名 的方式启动
        gef_path = self._find_gef()
        if gef_path:
            # 发送 file 命令加载文件
            result = self.execute_gef_command(f'file {filepath}')
            # 显示GEF上下文
            context_result = self.execute_gef_command('context')
            return f"Loaded with GEF: {filepath}\n{result}\n{context_result}"
        else:
            result = self.execute_gef_command(f'file {filepath}')
            return result
    
    def set_breakpoint(self, location: str) -> str:
        """设置断点"""
        return self.execute_gef_command(f'break {location}')
    
    def run(self, args: str = "") -> str:
        """运行程序 - 使用更长的超时时间捕获完整输出"""
        if args:
            return self.execute_gef_command(f'run {args}', extended_timeout=True)
        return self.execute_gef_command('run', extended_timeout=True)
    
    def continue_execution(self) -> str:
        """继续执行 - 使用更长的超时时间捕获完整输出"""
        return self.execute_gef_command('continue', extended_timeout=True)
    
    def step(self) -> str:
        """单步执行 (step into)"""
        return self.execute_gef_command('step')
    
    def next(self) -> str:
        """单步执行 (step over)"""
        return self.execute_gef_command('next')
    
    def examine_memory(self, address: str, count: int = 16) -> str:
        """检查内存"""
        return self.execute_gef_command(f'x/{count}xg {address}')
    
    def disassemble(self, location: str = None, count: int = 10) -> str:
        """反汇编代码"""
        if location:
            return self.execute_gef_command(f'disassemble {location},+{count*4}')
        return self.execute_gef_command(f'x/{count}i $pc')
    
    def get_registers(self) -> str:
        """获取寄存器状态"""
        return self.execute_gef_command('info registers')
    
    def get_backtrace(self) -> str:
        """获取调用栈"""
        return self.execute_gef_command('backtrace')
    
    def search_pattern(self, pattern: str) -> str:
        """搜索内存模式"""
        return self.execute_gef_command(f'search-pattern {pattern}')
    
    def vmmap(self) -> str:
        """显示虚拟内存映射"""
        return self.execute_gef_command('vmmap')
    
    def heap(self) -> str:
        """显示堆信息"""
        return self.execute_gef_command('heap')
    
    def telescope(self, address: str, count: int = 10) -> str:
        """望远镜命令 - 递归解引用"""
        return self.execute_gef_command(f'telescope {address} {count}')
    
    def close(self):
        """关闭会话"""
        with self._lock:
            if self._closed:
                return
            
            self._closed = True
            
            if self.master_fd:
                try:
                    os.close(self.master_fd)
                except Exception:
                    pass
                self.master_fd = None
            
            if self.child_pid:
                try:
                    os.kill(self.child_pid, signal.SIGTERM)
                    time.sleep(0.1)
                    os.kill(self.child_pid, signal.SIGKILL)
                except Exception:
                    pass
                self.child_pid = None
    
    def is_alive(self) -> bool:
        """检查会话是否活跃"""
        if self._closed or self.child_pid is None:
            return False
        try:
            os.kill(self.child_pid, 0)
            return True
        except OSError:
            self._closed = True
            return False


class SessionManager:
    """会话管理器"""
    MAX_SESSIONS = 4
    
    def __init__(self):
        self._sessions: Dict[str, GefSession] = {}
        self._lock = threading.Lock()
    
    def create_session(self, timeout: int = 10) -> Dict[str, Any]:
        """创建新会话"""
        with self._lock:
            if len(self._sessions) >= self.MAX_SESSIONS:
                return {
                    "success": False,
                    "error": f"Maximum session limit ({self.MAX_SESSIONS}) reached."
                }
            
            session_id = str(uuid.uuid4())[:8]
            
            try:
                session = GefSession(session_id, timeout)
                time.sleep(0.5)
                
                if session.is_alive():
                    self._sessions[session_id] = session
                    return {
                        "success": True,
                        "session_id": session_id,
                        "message": "GEF session created successfully"
                    }
                else:
                    return {
                        "success": False,
                        "error": "Failed to initialize GEF session"
                    }
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Error creating session: {str(e)}"
                }
    
    def get_session(self, session_id: str) -> Optional[GefSession]:
        """获取会话"""
        with self._lock:
            return self._sessions.get(session_id)
    
    def close_session(self, session_id: str) -> Dict[str, Any]:
        """关闭会话"""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return {
                    "success": False,
                    "error": f"Session {session_id} not found"
                }
            
            session.close()
            del self._sessions[session_id]
            
            return {
                "success": True,
                "message": f"Session {session_id} closed"
            }
    
    def list_sessions(self) -> Dict[str, Any]:
        """列出所有会话"""
        with self._lock:
            sessions_info = []
            for sid, session in self._sessions.items():
                sessions_info.append({
                    "session_id": sid,
                    "alive": session.is_alive(),
                    "target": session.target_file
                })
            
            return {
                "success": True,
                "total": len(sessions_info),
                "max_sessions": self.MAX_SESSIONS,
                "sessions": sessions_info
            }
    
    def close_all(self):
        """关闭所有会话"""
        with self._lock:
            for session in self._sessions.values():
                session.close()
            self._sessions.clear()


session_manager = SessionManager()


# ==================== MCP Tools ====================

def create_session_tool(params: dict) -> dict:
    """创建GEF会话"""
    timeout = params.get("timeout", 10)
    return session_manager.create_session(timeout=timeout)


def load_file_tool(params: dict) -> dict:
    """加载目标文件"""
    session_id = params.get("session_id")
    filepath = params.get("filepath")
    
    if not session_id:
        return {"success": False, "error": "session_id is required"}
    if not filepath:
        return {"success": False, "error": "filepath is required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    if not session.is_alive():
        return {"success": False, "error": f"Session {session_id} is not active"}
    
    result = session.load_file(filepath)
    return {"success": True, "session_id": session_id, "output": result}


def start_debugging_tool(params: dict) -> dict:
    """使用 gef 文件名 的方式启动调试 (Kali Linux新版本)"""
    session_id = params.get("session_id")
    filepath = params.get("filepath")
    
    if not session_id:
        return {"success": False, "error": "session_id is required"}
    if not filepath:
        return {"success": False, "error": "filepath is required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    if not session.is_alive():
        return {"success": False, "error": f"Session {session_id} is not active"}
    
    result = session.start_debugging(filepath)
    return {"success": True, "session_id": session_id, "output": result}


def execute_command_tool(params: dict) -> dict:
    """执行任意GEF/GDB命令"""
    session_id = params.get("session_id")
    command = params.get("command")
    
    if not session_id:
        return {"success": False, "error": "session_id is required"}
    if not command:
        return {"success": False, "error": "command is required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    if not session.is_alive():
        return {"success": False, "error": f"Session {session_id} is not active"}
    
    result = session.execute_gef_command(command)
    return {"success": True, "session_id": session_id, "output": result}


def set_breakpoint_tool(params: dict) -> dict:
    """设置断点"""
    session_id = params.get("session_id")
    location = params.get("location")
    
    if not session_id or not location:
        return {"success": False, "error": "session_id and location are required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    result = session.set_breakpoint(location)
    return {"success": True, "session_id": session_id, "output": result}


def run_tool(params: dict) -> dict:
    """运行程序"""
    session_id = params.get("session_id")
    args = params.get("args", "")
    
    if not session_id:
        return {"success": False, "error": "session_id is required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    result = session.run(args)
    return {"success": True, "session_id": session_id, "output": result}


def continue_tool(params: dict) -> dict:
    """继续执行"""
    session_id = params.get("session_id")
    
    if not session_id:
        return {"success": False, "error": "session_id is required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    result = session.continue_execution()
    return {"success": True, "session_id": session_id, "output": result}


def step_tool(params: dict) -> dict:
    """单步执行 (step into)"""
    session_id = params.get("session_id")
    
    if not session_id:
        return {"success": False, "error": "session_id is required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    result = session.step()
    return {"success": True, "session_id": session_id, "output": result}


def next_tool(params: dict) -> dict:
    """单步执行 (step over)"""
    session_id = params.get("session_id")
    
    if not session_id:
        return {"success": False, "error": "session_id is required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    result = session.next()
    return {"success": True, "session_id": session_id, "output": result}


def get_registers_tool(params: dict) -> dict:
    """获取寄存器状态"""
    session_id = params.get("session_id")
    
    if not session_id:
        return {"success": False, "error": "session_id is required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    result = session.get_registers()
    return {"success": True, "session_id": session_id, "output": result}


def disassemble_tool(params: dict) -> dict:
    """反汇编代码"""
    session_id = params.get("session_id")
    location = params.get("location")
    count = params.get("count", 10)
    
    if not session_id:
        return {"success": False, "error": "session_id is required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    result = session.disassemble(location, count)
    return {"success": True, "session_id": session_id, "output": result}


def examine_memory_tool(params: dict) -> dict:
    """检查内存"""
    session_id = params.get("session_id")
    address = params.get("address")
    count = params.get("count", 16)
    
    if not session_id or not address:
        return {"success": False, "error": "session_id and address are required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    result = session.examine_memory(address, count)
    return {"success": True, "session_id": session_id, "output": result}


def get_backtrace_tool(params: dict) -> dict:
    """获取调用栈"""
    session_id = params.get("session_id")
    
    if not session_id:
        return {"success": False, "error": "session_id is required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    result = session.get_backtrace()
    return {"success": True, "session_id": session_id, "output": result}


def vmmap_tool(params: dict) -> dict:
    """显示虚拟内存映射"""
    session_id = params.get("session_id")
    
    if not session_id:
        return {"success": False, "error": "session_id is required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    result = session.vmmap()
    return {"success": True, "session_id": session_id, "output": result}


def heap_tool(params: dict) -> dict:
    """显示堆信息"""
    session_id = params.get("session_id")
    
    if not session_id:
        return {"success": False, "error": "session_id is required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    result = session.heap()
    return {"success": True, "session_id": session_id, "output": result}


def telescope_tool(params: dict) -> dict:
    """望远镜命令 - 递归解引用内存"""
    session_id = params.get("session_id")
    address = params.get("address")
    count = params.get("count", 10)
    
    if not session_id or not address:
        return {"success": False, "error": "session_id and address are required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    result = session.telescope(address, count)
    return {"success": True, "session_id": session_id, "output": result}


def search_pattern_tool(params: dict) -> dict:
    """搜索内存模式"""
    session_id = params.get("session_id")
    pattern = params.get("pattern")
    
    if not session_id or not pattern:
        return {"success": False, "error": "session_id and pattern are required"}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}
    
    result = session.search_pattern(pattern)
    return {"success": True, "session_id": session_id, "output": result}


def list_sessions_tool(params: dict) -> dict:
    """列出所有会话"""
    return session_manager.list_sessions()


def close_session_tool(params: dict) -> dict:
    """关闭会话"""
    session_id = params.get("session_id")
    
    if not session_id:
        return {"success": False, "error": "session_id is required"}
    
    return session_manager.close_session(session_id)


# ==================== MCP Server Setup ====================

TOOLS = [
    Tool(
        name="create_session",
        description="""创建新的GEF调试会话。

自动加载GEF插件，初始化GDB环境。
最多支持4个并发会话。

Returns:
    session_id: 会话ID，用于后续操作
    
Example:
    create_session({"timeout": 60})""",
        inputSchema={
            "type": "object",
            "properties": {
                "timeout": {
                    "type": "integer",
                    "description": "命令执行超时时间(秒)",
                    "default": 10
                }
            }
        }
    ),
    Tool(
        name="load_file",
        description="""加载目标二进制文件到GEF会话。

Args:
    session_id: 会话ID
    filepath: 文件路径

Example:
    load_file({"session_id": "abc123", "filepath": "/path/to/binary"})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"},
                "filepath": {"type": "string", "description": "目标文件路径"}
            },
            "required": ["session_id", "filepath"]
        }
    ),
    Tool(
        name="start_debugging",
        description="""使用 gef 文件名 的方式启动调试 (Kali Linux新版本特性)。

这类似于在命令行执行: gef 文件名
会自动加载文件并显示GEF上下文信息。

Args:
    session_id: 会话ID
    filepath: 文件路径

Example:
    start_debugging({"session_id": "abc123", "filepath": "/path/to/binary"})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"},
                "filepath": {"type": "string", "description": "目标文件路径"}
            },
            "required": ["session_id", "filepath"]
        }
    ),
    Tool(
        name="execute_command",
        description="""执行任意GEF/GDB命令。

支持所有GEF和GDB原生命令，包括：
- GEF增强命令: heap, vmmap, telescope, search-pattern等
- GDB原生命令: break, run, continue, step等

Args:
    session_id: 会话ID
    command: 要执行的命令

Example:
    execute_command({"session_id": "abc123", "command": "info functions"})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"},
                "command": {"type": "string", "description": "GEF/GDB命令"}
            },
            "required": ["session_id", "command"]
        }
    ),
    Tool(
        name="set_breakpoint",
        description="""设置断点。

Args:
    session_id: 会话ID
    location: 断点位置 (函数名、地址、文件名:行号等)

Example:
    set_breakpoint({"session_id": "abc123", "location": "main"})
    set_breakpoint({"session_id": "abc123", "location": "*0x401000"})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"},
                "location": {"type": "string", "description": "断点位置"}
            },
            "required": ["session_id", "location"]
        }
    ),
    Tool(
        name="run",
        description="""运行程序。

Args:
    session_id: 会话ID
    args: 程序参数(可选)

Example:
    run({"session_id": "abc123", "args": "--help"})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"},
                "args": {"type": "string", "description": "程序参数", "default": ""}
            },
            "required": ["session_id"]
        }
    ),
    Tool(
        name="continue",
        description="""继续执行程序直到下一个断点或结束。

Args:
    session_id: 会话ID

Example:
    continue({"session_id": "abc123"})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"}
            },
            "required": ["session_id"]
        }
    ),
    Tool(
        name="step",
        description="""单步执行 (step into) - 进入函数内部。

Args:
    session_id: 会话ID

Example:
    step({"session_id": "abc123"})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"}
            },
            "required": ["session_id"]
        }
    ),
    Tool(
        name="next",
        description="""单步执行 (step over) - 跳过函数调用。

Args:
    session_id: 会话ID

Example:
    next({"session_id": "abc123"})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"}
            },
            "required": ["session_id"]
        }
    ),
    Tool(
        name="get_registers",
        description="""获取寄存器状态。

Args:
    session_id: 会话ID

Example:
    get_registers({"session_id": "abc123"})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"}
            },
            "required": ["session_id"]
        }
    ),
    Tool(
        name="disassemble",
        description="""反汇编代码。

Args:
    session_id: 会话ID
    location: 反汇编位置(可选，默认为当前PC)
    count: 指令数量(默认10)

Example:
    disassemble({"session_id": "abc123", "location": "main", "count": 20})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"},
                "location": {"type": "string", "description": "反汇编位置"},
                "count": {"type": "integer", "description": "指令数量", "default": 10}
            },
            "required": ["session_id"]
        }
    ),
    Tool(
        name="examine_memory",
        description="""检查内存内容。

Args:
    session_id: 会话ID
    address: 内存地址或寄存器
    count: 显示数量(默认16)

Example:
    examine_memory({"session_id": "abc123", "address": "$rsp", "count": 16})
    examine_memory({"session_id": "abc123", "address": "0x7fffffffd000", "count": 8})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"},
                "address": {"type": "string", "description": "内存地址或寄存器"},
                "count": {"type": "integer", "description": "显示数量", "default": 16}
            },
            "required": ["session_id", "address"]
        }
    ),
    Tool(
        name="get_backtrace",
        description="""获取函数调用栈。

Args:
    session_id: 会话ID

Example:
    get_backtrace({"session_id": "abc123"})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"}
            },
            "required": ["session_id"]
        }
    ),
    Tool(
        name="vmmap",
        description="""显示虚拟内存映射。

显示进程的内存布局，包括堆、栈、代码段、库等。

Args:
    session_id: 会话ID

Example:
    vmmap({"session_id": "abc123"})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"}
            },
            "required": ["session_id"]
        }
    ),
    Tool(
        name="heap",
        description="""显示堆信息。

显示堆的详细信息，包括chunk、arena等。

Args:
    session_id: 会话ID

Example:
    heap({"session_id": "abc123"})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"}
            },
            "required": ["session_id"]
        }
    ),
    Tool(
        name="telescope",
        description="""望远镜命令 - 递归解引用内存。

GEF特色命令，显示地址链的解引用结果。

Args:
    session_id: 会话ID
    address: 起始地址或寄存器
    count: 显示层数(默认10)

Example:
    telescope({"session_id": "abc123", "address": "$rsp", "count": 10})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"},
                "address": {"type": "string", "description": "起始地址或寄存器"},
                "count": {"type": "integer", "description": "显示层数", "default": 10}
            },
            "required": ["session_id", "address"]
        }
    ),
    Tool(
        name="search_pattern",
        description="""在内存中搜索模式。

Args:
    session_id: 会话ID
    pattern: 搜索模式(字符串或十六进制)

Example:
    search_pattern({"session_id": "abc123", "pattern": "/bin/sh"})
    search_pattern({"session_id": "abc123", "pattern": "\\x48\\x31\\xf6"})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"},
                "pattern": {"type": "string", "description": "搜索模式"}
            },
            "required": ["session_id", "pattern"]
        }
    ),
    Tool(
        name="list_sessions",
        description="""列出所有活跃的GEF会话。

Example:
    list_sessions({})""",
        inputSchema={
            "type": "object",
            "properties": {}
        }
    ),
    Tool(
        name="close_session",
        description="""关闭GEF会话。

Args:
    session_id: 会话ID

Example:
    close_session({"session_id": "abc123"})""",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话ID"}
            },
            "required": ["session_id"]
        }
    ),
]


mcp_server = Server("gef-mcp")


@mcp_server.list_tools()
async def list_tools():
    return TOOLS


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "create_session":
            result = create_session_tool(arguments or {})
        elif name == "load_file":
            result = load_file_tool(arguments or {})
        elif name == "start_debugging":
            result = start_debugging_tool(arguments or {})
        elif name == "execute_command":
            result = execute_command_tool(arguments or {})
        elif name == "set_breakpoint":
            result = set_breakpoint_tool(arguments or {})
        elif name == "run":
            result = run_tool(arguments or {})
        elif name == "continue":
            result = continue_tool(arguments or {})
        elif name == "step":
            result = step_tool(arguments or {})
        elif name == "next":
            result = next_tool(arguments or {})
        elif name == "get_registers":
            result = get_registers_tool(arguments or {})
        elif name == "disassemble":
            result = disassemble_tool(arguments or {})
        elif name == "examine_memory":
            result = examine_memory_tool(arguments or {})
        elif name == "get_backtrace":
            result = get_backtrace_tool(arguments or {})
        elif name == "vmmap":
            result = vmmap_tool(arguments or {})
        elif name == "heap":
            result = heap_tool(arguments or {})
        elif name == "telescope":
            result = telescope_tool(arguments or {})
        elif name == "search_pattern":
            result = search_pattern_tool(arguments or {})
        elif name == "list_sessions":
            result = list_sessions_tool(arguments or {})
        elif name == "close_session":
            result = close_session_tool(arguments or {})
        else:
            result = {"success": False, "error": f"Unknown tool: {name}"}
        
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"success": False, "error": str(e)}, indent=2))]


def main():
    import argparse
    import asyncio
    
    parser = argparse.ArgumentParser(description="GEF MCP Server - GDB Enhanced Features")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="sse",
                        help="Transport type (default: sse)")
    parser.add_argument("--host", default="0.0.0.0", help="SSE host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="SSE port (default: 8000)")
    args = parser.parse_args()
    
    if args.transport == "sse":
        try:
            from starlette.applications import Starlette
            from starlette.routing import Mount, Route
            from starlette.requests import Request
            from starlette.responses import PlainTextResponse
            import uvicorn
        except ImportError:
            print("Error: SSE mode requires starlette and uvicorn")
            print("Install with: pip install starlette uvicorn")
            sys.exit(1)
        
        sse_transport = SseServerTransport("/messages/")
        
        async def handle_sse(request: Request):
            try:
                async with sse_transport.connect_sse(
                    request.scope,
                    request.receive,
                    request._send,
                ) as streams:
                    if streams is None:
                        from starlette.responses import PlainTextResponse
                        return PlainTextResponse("Error: Failed to establish SSE connection", status_code=500)
                    read_stream, write_stream = streams
                    await mcp_server.run(
                        read_stream,
                        write_stream,
                        mcp_server.create_initialization_options(),
                    )
            except Exception as e:
                from starlette.responses import PlainTextResponse
                return PlainTextResponse(f"Error: {str(e)}", status_code=500)
        
        starlette_app = Starlette(
            debug=False,
            routes=[
                Route("/", endpoint=lambda request: PlainTextResponse("GEF MCP Server - GDB Enhanced Features")),
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", routes=[
                    Route("/", endpoint=lambda request: PlainTextResponse("MCP Messages"), methods=["GET"]),
                ]),
            ],
        )
        
        async def asgi_app(scope, receive, send):
            path = scope.get("path", "/")
            method = scope.get("method", "GET")
            
            if path == "/" or path.startswith("/sse"):
                await starlette_app(scope, receive, send)
            elif path.startswith("/messages/"):
                await sse_transport.handle_post_message(scope, receive, send)
            else:
                from starlette.responses import PlainTextResponse
                response = PlainTextResponse("Not Found", status_code=404)
                await response(scope, receive, send)
        
        print("""
╔═══════════════════════════════════════════════════════════════╗
║                                                               ║
║   ██████╗ ███████╗███████╗    ███╗   ███╗ ██████╗██████╗      ║
║  ██╔════╝ ██╔════╝██╔════╝    ████╗ ████║██╔════╝██╔══██╗     ║
║  ██║  ███╗█████╗  █████╗      ██╔████╔██║██║     ██████╔╝     ║
║  ██║   ██║██╔══╝  ██╔══╝      ██║╚██╔╝██║██║     ██╔═══╝      ║
║  ╚██████╔╝██║     ██║         ██║ ╚═╝ ██║╚██████╗██║          ║
║   ╚═════╝ ╚═╝     ╚═╝         ╚═╝     ╚═╝ ╚═════╝╚═╝          ║
║                                                               ║
║              GEF MCP Server v1.0 - Kali Linux                 ║
║         GDB Enhanced Features Model Context Protocol          ║
║                                                               ║
╠═══════════════════════════════════════════════════════════════╣
║  MCP SSE Endpoint:  http://{host}:{port}/sse                  ║
║  Messages Endpoint: http://{host}:{port}/messages/             ║
╚═══════════════════════════════════════════════════════════════╝
        """.format(host=args.host, port=args.port))
        
        print(f"GEF MCP SSE endpoint: http://{args.host}:{args.port}/sse")
        print(f"Messages endpoint: http://{args.host}:{args.port}/messages/")
        uvicorn.run(asgi_app, host=args.host, port=args.port)
    else:
        print("Starting GEF MCP stdio server...")
        
        async def run():
            async with stdio_server() as (read_stream, write_stream):
                await mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp_server.create_initialization_options()
                )
        
        asyncio.run(run())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nShutting down GEF MCP Server...")
        session_manager.close_all()
    except Exception as e:
        print(f"Error: {e}")
        session_manager.close_all()
        sys.exit(1)
