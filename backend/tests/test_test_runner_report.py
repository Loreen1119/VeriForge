"""产品侧契约：`run_allowlisted` 的 `output`（展示/落库）与 `report`（判定）双通道。

**本文件不 import judge**：`scripts/benchmark/` 被 `.gitignore:62` 排除、公开仓里不存在，
backend 的测试一旦 import 它，干净检出 / CI 上必然
`ModuleNotFoundError: judge`（collection error）。judge 侧的集成断言在同名脚本目录的
`scripts/benchmark/test_judge_report_channel.py`，随 benchmark 目录一起不进 VCS。

钉住的两件事：
1. `output` 仍是**末** `OUTPUT_DISPLAY_LIMIT` 字符（展示护栏，不许放开）；
   `report.nodes` 由**未截断全文**解析，大用例集下节点一个不少。
2. 解析只按 `" - "` 切：含空格 / 含换行转义（字面 ``\\n``）的参数化 nodeid 不被切断。

每个用例都真实执行到断言：没有 `if <前置>:` 守卫、没有 skip/xfail。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]          # backend/
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import test_runner  # noqa: E402

TARGET_FILE = "tests/test_oversized_report.py"
TARGET_TEST = "test_case_000_fail_to_pass_target"
F2P = f"{TARGET_FILE}::{TARGET_TEST}"
FILLER_COUNT = 419
TOTAL_TESTS = FILLER_COUNT + 1                          # 420
DISPLAY_LIMIT = test_runner.OUTPUT_DISPLAY_LIMIT


# --------------------------------------------------------------------------
# 15058 最小复现：420 个全过用例、-rA 摘要远超 2 万字符、F2P 目标在最前面
# --------------------------------------------------------------------------


def _oversized_source() -> str:
    """420 个全过测试的源码：第 1 个是 F2P 目标，其余 419 个带长 padding。"""
    lines = [
        f"def {TARGET_TEST}():",
        "    assert True",
        "",
    ]
    padding = "pad_" * 8                                # 32 字符，把单行摘要撑到 ~90 字符
    for index in range(1, FILLER_COUNT + 1):
        lines += [
            f"def test_case_{index:03d}_padding_{padding}():",
            "    assert True",
            "",
        ]
    return "\n".join(lines)


@pytest.fixture(scope="module")
def oversized_run(tmp_path_factory):
    """真跑一次 420 个用例的 pytest（本模块只跑一次，省时间）。"""
    tmp_path = tmp_path_factory.mktemp("oversized-report")
    target_dir = tmp_path / "tests"
    target_dir.mkdir()
    (target_dir / "test_oversized_report.py").write_text(_oversized_source(), encoding="utf-8")

    cmd = f'"{sys.executable}" -m pytest {TARGET_FILE} -rA -q -p no:cacheprovider'
    result = test_runner.run_allowlisted(cmd, tmp_path, allowed_commands=[cmd], timeout_seconds=120)
    return {"tmp_path": tmp_path, "command": cmd, "result": result}


def test_超长通过输出_展示字段仍保留两万字符护栏(oversized_run):
    """`output` 是展示/落库字段，末 2 万字符护栏是设计，不许因为判定改造把它放开。"""
    result = oversized_run["result"]
    assert result["status"] == "passed"
    assert result["exit_code"] == 0
    assert DISPLAY_LIMIT == 20000
    assert len(result["output"]) == DISPLAY_LIMIT


def test_超长通过输出_report从未截断全文解析出全部420节点(oversized_run):
    """判定通道：`report` 由未截断全文构建 → 420 个节点一个不少，最前面的 F2P 判为 passed。"""
    report = oversized_run["result"]["report"]
    assert report["kind"] == "pytest-summary"
    assert report["output_truncated"] is True
    assert report["output_chars"] > DISPLAY_LIMIT
    assert report["display_limit"] == DISPLAY_LIMIT
    assert report["node_count"] == TOTAL_TESTS
    assert F2P in report["nodes"], "结构化 report 必须包含文件最前面那个节点"
    assert report["nodes"][F2P] == "passed"


def test_超长通过输出_展示字段里目标节点与前段行已被砍掉(oversized_run):
    """伪影的直接事实证据（不需要 judge 参与）：目标 nodeid 在 `output` 里已经不存在了。

    旧逻辑拿 `output` 做 `-rA` 正则反推节点状态，必然把这个节点读成「找不到」→
    正确的补丁被记成「方案错」（15058）。
    """
    result = oversized_run["result"]
    assert F2P not in result["output"], "目标节点若还在展示字段里，这条用例就证明不了伪影"
    assert result["output"].count("PASSED ") < TOTAL_TESTS, "展示字段里可见的 PASSED 行应明显少于全量"


# --------------------------------------------------------------------------
# 14463 模式：含空格 / 含换行转义的参数化 nodeid 不得被切断
# --------------------------------------------------------------------------

SPACED_NODEID = "testing/test_assertion.py::test_cmp[a b c-False]"
SPACED_TEXT = (
    "PASSED testing/test_assertion.py::test_cmp[a b c-False]\n"
    "FAILED testing/test_assertion.py::test_other - AssertionError: boom\n"
)
# pytest 把参数里的换行渲染成字面 `\n`（反斜杠 + n），整条 nodeid 仍在**同一行**；
# `_norm_nodeid` 会把反斜杠归一成正斜杠（Windows 路径同口径），两侧一致所以查得到。
NEWLINE_NODEID = r"testing/test_nl.py::test_nl[a\nb]"
NEWLINE_TEXT = "PASSED testing/test_nl.py::test_nl[a\\nb]\n"


def test_含空格参数化nodeid完整保留():
    """只按 `" - "` 切 → `[a b c-False]` 里的空格不会被切成 `test_cmp[a`（14463 的漏点）。"""
    status_map = test_runner.parse_pytest_node_statuses(SPACED_TEXT)
    assert SPACED_NODEID in status_map
    assert status_map[SPACED_NODEID] == "passed"
    # 失败行尾部的 `" - 原因"` 仍要切掉，否则 nodeid 会带上 AssertionError
    assert status_map["testing/test_assertion.py::test_other"] == "failed"


def test_含换行参数化nodeid仍是单行且能取回():
    """换行转义成字面 `\\n` 后不得被当成换行拆成两条，也不得被截断。"""
    status_map = test_runner.parse_pytest_node_statuses(NEWLINE_TEXT)
    assert len(status_map) == 1, "一条 PASSED 行只能产出一个节点"
    assert "testing/test_nl.py::test_nl[a/nb]" in status_map
    assert status_map["testing/test_nl.py::test_nl[a/nb]"] == "passed"
    assert not any(key.startswith("b]") for key in status_map), "不许把转义换行拆到下一行"


# --------------------------------------------------------------------------
# 超时分支也要产出 report（判定层不能因为超时拿不到结构）
# --------------------------------------------------------------------------


def test_超时分支report形状契约(tmp_path):
    """超时路径同样落 `report` 键，形状与正常路径一致（老消费方读 `output` 不炸）。"""
    inner = "print('x'*30000);import time;time.sleep(5)"
    cmd = f'"{sys.executable}" -c "{inner}"'
    result = test_runner.run_allowlisted(cmd, tmp_path, allowed_commands=[cmd], timeout_seconds=1)

    assert result["status"] == "timed_out"
    assert result["timed_out"] is True
    assert "report" in result
    assert set(result["report"]) == {
        "kind", "nodes", "node_count", "output_chars", "output_truncated", "display_limit",
    }
    assert isinstance(result["report"]["nodes"], dict)
    assert result["report"]["display_limit"] == DISPLAY_LIMIT
    # 只用 `<=`：Windows 下 `TimeoutExpired.stdout` 可能是 None，捕不到输出也不能判失败
    assert len(result["output"]) <= DISPLAY_LIMIT
