"""生成期自检：补丁先在临时 worktree 上试打，失败则按 git 指出的行号回读真实内容再重试。

背景（2026-10-18 评测）：apply 失败的主因不是 hunk 行数算错（--recount 早已兜住），
而是模型在它没被提供过的行区间凭记忆写上下文。放大检索窗口无效，只能回读真实文件。
"""
import subprocess

import pytest

from patch_generator import LLMPatchGenerator, _self_check
from schemas import CodeContext, PatchGenerationContext, ProposedChange
from worktree_service import probe_apply

_GOOD = "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def a():\n-    return 1\n+    return 2\n"
# 上下文里的 "return 9" 在文件里不存在 → 四档降级链全失败，且能报出 app.py:2
_BAD = "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def a():\n-    return 9\n+    return 2\n"
# 计划还要改 lib.py，但这份补丁只动了 app.py → 完备性不过
_GOOD_ONE_FILE = _GOOD
_GOOD_BOTH = (
    "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def a():\n-    return 1\n+    return 2\n"
    "--- a/lib.py\n+++ b/lib.py\n@@ -1,2 +1,2 @@\n def b():\n-    return 0\n+    return 1\n"
)


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@e.c"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / "app.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
    return root


def _ctx(git_root=""):
    return PatchGenerationContext(
        task_id=7,
        repository_id=1,
        plan_id=1,
        issue_title="a() 返回值不对",
        issue_body="a() 应返回 2。",
        plan_summary="改 app.py 的返回值",
        proposed_changes=[ProposedChange(path="app.py", description="返回 2")],
        code_context=[CodeContext(path="app.py", line_start=1, line_end=2,
                                  matched_line=2, snippet="def a():\n    return 1\n")],
        base_commit=None,
        patch_revision=1,
        reviewer_feedback=None,
        git_root=str(git_root),
    )


def _ctx_two_files(git_root=""):
    """计划改两个文件（app.py + lib.py），完备性检查用。"""
    return PatchGenerationContext(
        task_id=8,
        repository_id=1,
        plan_id=1,
        issue_title="a()/b() 返回值不对",
        issue_body="a() 应返回 2，b() 应返回 1。",
        plan_summary="同时改 app.py 与 lib.py",
        proposed_changes=[
            ProposedChange(path="app.py", description="返回 2"),
            ProposedChange(path="lib.py", description="返回 1"),
        ],
        code_context=[
            CodeContext(path="app.py", line_start=1, line_end=2,
                        matched_line=2, snippet="def a():\n    return 1\n"),
            CodeContext(path="lib.py", line_start=1, line_end=2,
                        matched_line=2, snippet="def b():\n    return 0\n"),
        ],
        base_commit=None,
        patch_revision=1,
        reviewer_feedback=None,
        git_root=str(git_root),
    )


def test_probe_apply_ok_leaves_no_temp_worktree(tmp_path):
    root = _repo(tmp_path)
    result = probe_apply(str(root), None, _GOOD, tag="t-ok")
    assert result["ok"] is True
    assert result["mode"]
    probes = root / ".codeforge" / "probes"
    assert not probes.exists() or not list(probes.iterdir())


def test_probe_apply_failure_carries_real_file_lines(tmp_path):
    root = _repo(tmp_path)
    result = probe_apply(str(root), None, _BAD, tag="t-bad")
    assert result["ok"] is False
    # 报错要能指出文件与行号，否则回喂没有定位价值
    assert "app.py" in result["error"]
    # hint 要带上真实文件内容，模型才有可逐字复制的素材
    assert "return 1" in result["hint"]
    assert "app.py" in result["hint"]


@pytest.mark.asyncio
async def test_generator_retries_with_real_context_after_probe_failure(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    gen = LLMPatchGenerator(api_key="test-key", max_retries=1)
    replies = [_BAD, _GOOD]
    prompts: list[str] = []

    async def fake(messages):
        prompts.append(messages[-1]["content"])
        return replies.pop(0)

    monkeypatch.setattr(gen, "_call", fake)
    res = await gen.generate(_ctx(root))

    assert res.provider == "deepseek"
    assert "+    return 2" in res.diff
    # 第一次 Prompt 不带重试段，第二次才带（第一次的行为与改造前逐字一致）
    assert "# Previous attempt failed" not in prompts[0]
    assert "# Previous attempt failed" in prompts[1]
    # 重试素材里必须有真实文件内容，而不只是一句 "patch does not apply"
    assert "return 1" in prompts[1]
    assert res.risk_notes and "无法应用" in res.risk_notes[0]


@pytest.mark.asyncio
async def test_generator_does_not_retry_when_patch_applies(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    gen = LLMPatchGenerator(api_key="test-key", max_retries=2)
    calls = []

    async def fake(messages):
        calls.append(1)
        return _GOOD

    monkeypatch.setattr(gen, "_call", fake)
    res = await gen.generate(_ctx(root))
    assert len(calls) == 1
    assert res.risk_notes == []


@pytest.mark.asyncio
async def test_self_check_exhausted_returns_model_patch_not_fake(tmp_path, monkeypatch):
    """自检连不过也不能退成 Fake。

    Fake 补丁会被 judge 判成 generator_fallback 整条剔除，等于把「补丁打不上」
    改写成「模型没产出」，可判定池会凭空缩水（2026-10-18 实测一轮从 17 条掉到 6 条）。
    """
    root = _repo(tmp_path)
    gen = LLMPatchGenerator(api_key="test-key", max_retries=1)

    async def always_bad(messages):
        return _BAD

    monkeypatch.setattr(gen, "_call", always_bad)
    res = await gen.generate(_ctx(root))

    assert res.provider == "deepseek"
    assert res.diff.strip()
    assert any("自检" in note for note in res.risk_notes)


@pytest.mark.asyncio
async def test_self_check_skipped_without_git_root(monkeypatch):
    """没有 git_root（测试环境 / Fake 生成器）时绝不能触发 worktree 操作。"""
    def boom(*args, **kwargs):
        raise AssertionError("git_root 为空时不应调用 probe_apply")

    monkeypatch.setattr("worktree_service.probe_apply", boom)
    gen = LLMPatchGenerator(api_key="test-key", max_retries=0)

    async def fake(messages):
        return _BAD

    monkeypatch.setattr(gen, "_call", fake)
    res = await gen.generate(_ctx())
    assert res.provider == "deepseek"
    assert res.risk_notes == []


# ---- 完备性回喂（2026-10-21 [K] 对照复盘 → P0-2 动作 A） ----
# 背景：主跑 14463 产物只删一行 import、漏改计划里的另一个文件，F2P 全挂。
# 「方案对但没写完」必须在生成期就被拦下并带「你漏改了 X 文件」重试。

def test_self_check_flags_missing_planned_files():
    """补丁漏改计划文件 → 提示必须点名漏改的文件。"""
    notes: list[str] = []
    hint = _self_check(_ctx_two_files(), _GOOD_ONE_FILE, 0, notes)
    assert "lib.py" in hint
    assert "Proposed changes" in hint
    assert notes and "漏改" in notes[0]


def test_self_check_completeness_runs_without_git_root(monkeypatch):
    """完备性是纯语法检查：没有 git_root 也照跑，且绝不触发 worktree 操作。"""
    def boom(*args, **kwargs):
        raise AssertionError("git_root 为空时不应调用 probe_apply")

    monkeypatch.setattr("worktree_service.probe_apply", boom)
    notes: list[str] = []
    hint = _self_check(_ctx_two_files(), _GOOD_ONE_FILE, 0, notes)
    assert "lib.py" in hint


def test_self_check_passes_when_all_planned_files_touched():
    notes: list[str] = []
    assert _self_check(_ctx_two_files(), _GOOD_BOTH, 0, notes) == ''
    assert notes == []


@pytest.mark.asyncio
async def test_generator_retries_with_missing_file_hint(monkeypatch):
    """漏改计划文件 → 带「你漏改了 X 文件」重试，补齐后正常返回。"""
    gen = LLMPatchGenerator(api_key="test-key", max_retries=1)
    replies = [_GOOD_ONE_FILE, _GOOD_BOTH]
    prompts: list[str] = []

    async def fake(messages):
        prompts.append(messages[-1]["content"])
        return replies.pop(0)

    monkeypatch.setattr(gen, "_call", fake)
    res = await gen.generate(_ctx_two_files())

    assert res.provider == "deepseek"
    assert "lib.py" in res.diff
    assert "# Previous attempt failed" in prompts[1]
    assert "漏改" in prompts[1] and "lib.py" in prompts[1]
    assert any("漏改" in note for note in res.risk_notes)


@pytest.mark.asyncio
async def test_generator_returns_last_patch_when_completeness_never_passes(monkeypatch):
    """自检连不过也不退 Fake（同 probe 语义）：如实返回最后一版补丁。"""
    gen = LLMPatchGenerator(api_key="test-key", max_retries=1)

    async def always_incomplete(messages):
        return _GOOD_ONE_FILE

    monkeypatch.setattr(gen, "_call", always_incomplete)
    res = await gen.generate(_ctx_two_files())

    assert res.provider == "deepseek"
    assert res.diff.strip()
    assert any("自检" in note for note in res.risk_notes)


@pytest.mark.asyncio
async def test_generator_prefers_applicable_over_corrupt_retries(tmp_path, monkeypatch):
    """保留策略（5A 教训）：可应用但不完整的补丁绝不能被后续 corrupt 重试覆盖。"""
    root = _repo(tmp_path)
    gen = LLMPatchGenerator(api_key="test-key", max_retries=1)
    replies = [_GOOD_ONE_FILE, _BAD]  # 先可应用（但漏改），后 corrupt

    async def fake(messages):
        return replies.pop(0)

    monkeypatch.setattr(gen, "_call", fake)
    res = await gen.generate(_ctx_two_files(root))

    assert "+    return 2" in res.diff   # 保住第一次可应用的版本
    assert "return 9" not in res.diff    # corrupt 版本没有顶掉它
