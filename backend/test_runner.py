"""Allowlisted syntax and test execution inside an isolated worktree.

返回契约（`run_allowlisted`）里有两个字段，用途严格分开，别混用：

- ``output``：**展示 / 落库**字段，只保留全文末 ``OUTPUT_DISPLAY_LIMIT`` 字符。
  HTTP 响应与前端 ``<pre>`` 都是全量渲染，放开护栏等于把响应体放大 5~20 倍。
- ``report``：**判定**字段，由**未截断**全文解析出的结构化产物
  （``kind`` / ``nodes`` / ``node_count`` / ``output_chars`` / ``output_truncated``）。

判定必须读 ``report``，不许再对 ``output`` 做正则解析：大用例集下 ``-rA`` 摘要前段的
PASSED/FAILED 行会被末尾护栏砍掉，从截断文本反推节点状态必然把「过了」读成「找不到」
（2026-10 主跑 15058 / 14463 就是这么被误判成「方案错」/「零回归」的）。
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

# 展示 / 落库护栏（字符数）。判定通道不依赖它 —— 见模块 docstring。
OUTPUT_DISPLAY_LIMIT = 20000

_SUMMARY_KEYWORDS = ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS")
_KEYWORD_STATUS = {
    "PASSED": "passed",
    "FAILED": "failed",
    "ERROR": "error",
    "SKIPPED": "skipped",
    "XFAIL": "skipped",
    "XPASS": "passed",
}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _norm_nodeid(nodeid: str) -> str:
    """nodeid 归一化：反斜杠转正斜杠、去前导 `./`、压掉重复空白（与 judge._norm_nodeid 同口径）。"""
    text = str(nodeid).strip().replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    return " ".join(text.split())


def parse_pytest_node_statuses(output: str) -> dict[str, str]:
    """把 pytest `-rA` 短摘要解析成 {nodeid: 状态}，状态取 passed/failed/error/skipped。

    认这几种行：
        PASSED tests/test_a.py::test_x
        FAILED tests/test_a.py::test_y - AssertionError: ...
        ERROR tests/test_a.py::test_z
        SKIPPED [1] tests/test_a.py:12: reason

    这是**判定层唯一的解析入口**（`scripts/benchmark/judge.py` 也 import 它）：
    两侧共用一份实现，才不会出现「执行侧说过了、判定侧说找不到」。
    参数化 nodeid 自带空格（`...::test_pp[c d]`），所以**只按 `" - "` 切**；
    按空格切会把 `test_pp[c d]` 截成 `test_pp[c`，输出再完整也永远查不到节点。
    """
    result: dict[str, str] = {}
    for raw in (output or "").splitlines():
        line = _ANSI_RE.sub("", raw).strip()
        if not line:
            continue
        head, _, rest = line.partition(" ")
        if head not in _SUMMARY_KEYWORDS:
            continue
        rest = rest.strip()
        if rest.startswith("["):                        # SKIPPED [1] path:12: reason
            _, _, rest = rest.partition("]")
            nodeid = rest.strip().split(":")[0].strip()
        else:
            nodeid = rest.split(" - ", 1)[0].strip()    # 只按 " - " 切：nodeid 自己可能含空格
        if nodeid and ("::" in nodeid or nodeid.endswith(".py")):
            result[_norm_nodeid(nodeid)] = _KEYWORD_STATUS[head]
    return result


def _build_report(full_output: str) -> dict:
    """从未截断全文构建结构化报告（判定通道的输入，`output` 不参与判定）。"""
    nodes = parse_pytest_node_statuses(full_output)
    return {
        "kind": "pytest-summary" if nodes else "none",
        "nodes": nodes,
        "node_count": len(nodes),
        "output_chars": len(full_output),
        "output_truncated": len(full_output) > OUTPUT_DISPLAY_LIMIT,
        "display_limit": OUTPUT_DISPLAY_LIMIT,
    }


def syntax_check(worktree: Path, changed_files: list[str]) -> tuple[bool, str]:
    for relative in changed_files:
        path = worktree / relative
        if path.suffix == ".py":
            result = subprocess.run(["python", "-m", "py_compile", str(path)], cwd=worktree, capture_output=True, text=True)
            if result.returncode:
                return False, result.stderr or result.stdout
    return True, ""


def run_allowlisted(command: str, worktree: Path, allowed_commands: list[str] | None = None, timeout_seconds: int = 300) -> dict:
    """跑一条白名单命令；`output` 是展示/落库字段，`report` 才是判定输入（见模块 docstring）。"""
    if allowed_commands is None or command not in allowed_commands:
        return {"status": "environment_error", "exit_code": None, "output": "命令不在白名单中", "timed_out": False, "environment_error": "命令不在白名单中", "duration_ms": 0, "report": _build_report("")}
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=worktree, shell=True, capture_output=True, text=True, timeout=min(timeout_seconds, 300))
        full = result.stdout + result.stderr
        return {"status": "passed" if result.returncode == 0 else "failed", "exit_code": result.returncode, "output": full[-OUTPUT_DISPLAY_LIMIT:], "timed_out": False, "environment_error": None, "duration_ms": int((time.monotonic() - started) * 1000), "report": _build_report(full)}
    except subprocess.TimeoutExpired as exc:
        full = (exc.stdout or "") + (exc.stderr or "")
        return {"status": "timed_out", "exit_code": None, "output": full[-OUTPUT_DISPLAY_LIMIT:], "timed_out": True, "environment_error": None, "duration_ms": int((time.monotonic() - started) * 1000), "report": _build_report(full)}
    except (OSError, ValueError) as exc:
        return {"status": "environment_error", "exit_code": None, "output": str(exc), "timed_out": False, "environment_error": str(exc), "duration_ms": int((time.monotonic() - started) * 1000), "report": _build_report("")}
