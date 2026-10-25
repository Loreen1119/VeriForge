"""VeriForge 的 MCP 工具面：把「代码检索」与「白名单测试执行」暴露成 Agent 可自主调用的能力单元。

与 HTTP API 的差别不在能力，而在**调用方**：HTTP 面向前端与外部脚本，MCP 面向 Agent。
所以这一层的设计约束是：

1. **不新增写入路径**。五个工具里只有一个会执行命令，而且只在
   `.codeforge/worktrees/task-{id}` 里执行 —— 与 HTTP 面 apply / test 走的是同一条边界，
   原始工作区永远只读。
2. **不接受自由路径与自由命令**。源码范围永远取自仓库配置的 `allowed_source_roots`，
   命令必须精确命中 `allowed_test_commands`；Agent 无法用参数把边界放宽。
3. **不返回仓库绝对路径**。一旦把绝对路径交给 Agent，配了文件系统工具的 Agent 就能
   绕开 `allowed_source_roots` 直接读整个仓库 —— 边界是配置说了算，不是调用方说了算。
4. **拒绝要说人话**。越界一律返回结构化的 `{ok: False, error: 'boundary_rejected', ...}`，
   让 Agent 知道该改什么，而不是吃一个栈。

入口：`python -m mcp_server`（stdio）。
"""
from __future__ import annotations

import json
from typing import Any

from code_search import read_code as _read_code
from code_search import search_repository
from repository_service import RepositoryValidationError, inspect_repository
from task_service import get_repository
from task_service import list_repositories as _list_repositories
from test_runner import run_allowlisted
from worktree_service import worktree_path

SERVER_NAME = 'veriforge'
SERVER_VERSION = '0.1.0'

SERVER_INSTRUCTIONS = (
    'VeriForge 的代码维护能力。先调用 list_repositories 拿到仓库 id 与它的安全边界；'
    '源码范围与可执行命令都以仓库配置为准，不接受调用方指定路径或任意命令。'
)


def _boundary(message: str, **extra: Any) -> dict[str, Any]:
    """越界/不满足前提的统一返回。不抛异常：Agent 需要可读的失败原因。"""
    return {'ok': False, 'error': 'boundary_rejected', 'message': message, **extra}


async def _repository(repository_id: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    row = await get_repository(repository_id)
    if row is None:
        return None, _boundary(f'仓库 {repository_id} 不存在', repository_id=repository_id)
    data = dict(row)
    data['allowed_source_roots'] = json.loads(data['allowed_source_roots'])
    data['allowed_test_commands'] = json.loads(data['allowed_test_commands'])
    return data, None


async def list_repositories() -> dict[str, Any]:
    """列出已配置仓库及其安全边界。

    这是入口工具：先知道有哪些仓库、每个仓库允许检索哪些源码目录、允许跑哪些测试命令。
    刻意**不返回仓库的本地绝对路径** —— 边界由配置决定，不能靠把路径交给调用方来"让它自己去读"。
    """
    rows = await _list_repositories()
    return {
        'ok': True,
        'repositories': [
            {
                'id': row['id'],
                'name': row['name'],
                'default_branch': row['default_branch'],
                'allowed_source_roots': json.loads(row['allowed_source_roots']),
                'allowed_test_commands': json.loads(row['allowed_test_commands']),
            }
            for row in rows
        ],
    }


async def search_code(repository_id: int, query: str = '', filename: str | None = None,
                      symbol: str | None = None, max_results: int = 10,
                      context_lines: int = 2) -> dict[str, Any]:
    """在仓库允许的源码目录内检索代码，返回带路径、符号、行号的片段。

    没有 repository_root / path 参数：检索根目录与范围永远来自仓库配置。
    query / filename / symbol 至少给一个；参数超范围会被拒绝，不会静默截断。
    """
    repository, error = await _repository(repository_id)
    if error is not None:
        return error
    try:
        info = inspect_repository(repository['local_path'], repository['allowed_source_roots'])
        results = search_repository(
            info['git_root'], query,
            filename=filename, symbol=symbol,
            allowed_source_roots=repository['allowed_source_roots'],
            max_results=max_results, context_lines=context_lines,
        )
    except (RepositoryValidationError, ValueError, OSError) as exc:
        return _boundary(str(exc), repository_id=repository_id)
    return {'ok': True, 'repository_id': repository_id, 'count': len(results), 'results': results}


async def read_code(repository_id: int, path: str, start_line: int = 1,
                    end_line: int | None = None, symbol: str | None = None) -> dict[str, Any]:
    """按路径、行范围或符号读取源码片段；路径必须落在仓库允许的源码目录内。"""
    repository, error = await _repository(repository_id)
    if error is not None:
        return error
    try:
        info = inspect_repository(repository['local_path'], repository['allowed_source_roots'])
        content = _read_code(
            info['git_root'], path,
            start_line=start_line, end_line=end_line, symbol=symbol,
            allowed_source_roots=repository['allowed_source_roots'],
        )
    except (RepositoryValidationError, ValueError, OSError) as exc:
        return _boundary(str(exc), repository_id=repository_id, path=path)
    return {'ok': True, 'repository_id': repository_id, **content}


async def list_allowed_test_commands(repository_id: int) -> dict[str, Any]:
    """仓库白名单里的测试命令。跑测试前先看这里，不要猜命令 —— 不在白名单里的一律会被拒。"""
    repository, error = await _repository(repository_id)
    if error is not None:
        return error
    return {'ok': True, 'repository_id': repository_id,
            'allowed_test_commands': repository['allowed_test_commands']}


async def run_allowed_test(repository_id: int, task_id: int, command: str) -> dict[str, Any]:
    """在任务的隔离 Worktree 里执行一条白名单测试命令，返回退出码、输出与耗时。

    两条硬约束：
    - 只在 `.codeforge/worktrees/task-{task_id}` 里执行。Worktree 不存在（补丁还没应用）
      就直接拒绝，**不做"回退到仓库根目录"这种降级** —— 那会污染原始工作区。
    - command 必须精确命中仓库白名单。
    """
    repository, error = await _repository(repository_id)
    if error is not None:
        return error
    if command not in repository['allowed_test_commands']:
        return _boundary('命令不在仓库白名单中', command=command,
                         allowed_test_commands=repository['allowed_test_commands'])
    worktree = worktree_path(repository['local_path'], task_id)
    if not worktree.is_dir():
        return _boundary(f'任务 {task_id} 的隔离 Worktree 不存在，补丁尚未应用', task_id=task_id)
    result = run_allowlisted(command, worktree, repository['allowed_test_commands'])
    return {'ok': True, 'repository_id': repository_id, 'task_id': task_id,
            'worktree': f'.codeforge/worktrees/task-{task_id}',
            'command': command, **result}


def build_server():
    """构造 FastMCP 服务实例。

    延迟导入 fastmcp：上面的工具函数是普通 async 函数，不装 fastmcp 的环境里也能被
    单测直接调用（边界规则本来就该独立于传输层被测）。
    """
    from fastmcp import FastMCP

    server = FastMCP(SERVER_NAME, version=SERVER_VERSION, instructions=SERVER_INSTRUCTIONS)
    for tool in (list_repositories, search_code, read_code, list_allowed_test_commands, run_allowed_test):
        server.tool()(tool)
    return server


def main() -> None:
    build_server().run()


if __name__ == '__main__':
    main()
