"""Tests for P1-1 LLMPatchGenerator (DeepSeek-compatible, with retry + fallback)."""
import shutil
import subprocess
import pytest
from schemas import PatchGenerationContext, ProposedChange, CodeContext
import patch_generator
from patch_generator import LLMPatchGenerator, _extract_diff


def _make_ctx():
    return PatchGenerationContext(
        task_id=1,
        repository_id=1,
        plan_id=1,
        issue_title="normalize_email does not handle full-width @",
        issue_body="全角 ＠ 没有被统一成半角 @，导致邮箱比对失败。",
        plan_summary="审查 src/app.py 的 normalize_email，补充全角 ＠ 的归一化。",
        proposed_changes=[ProposedChange(path="src/app.py", description="归一化全角 ＠")],
        code_context=[CodeContext(path="src/app.py", line_start=1, line_end=3,
                                  matched_line=2, snippet="def normalize_email(e):\n    return e.strip()\n")],
        base_commit=None,
        patch_revision=1,
        reviewer_feedback=None,
    )


_VALID_DIFF = (
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,3 +1,4 @@\n"
    " def normalize_email(e):\n"
    "-    return e.strip()\n"
    "+    e = e.replace('＠', '@')\n"
    "+    return e.strip()\n"
)


@pytest.mark.asyncio
async def test_llm_returns_parsed_diff(monkeypatch):
    """A well-formed LLM response is parsed into a valid PatchGenerationResult."""
    gen = LLMPatchGenerator(api_key="test-key")
    async def fake(messages):
        return _VALID_DIFF
    monkeypatch.setattr(gen, "_call", fake)
    res = await gen.generate(_make_ctx())
    assert res.provider == "deepseek"
    assert res.diff.rstrip() == _VALID_DIFF.rstrip()
    assert "src/app.py" in res.changed_files
    assert res.risk_notes == []


@pytest.mark.asyncio
async def test_llm_falls_back_on_exception(monkeypatch):
    """Network/HTTP errors must fall back to Fake, never crash the pipeline."""
    gen = LLMPatchGenerator(api_key="test-key", max_retries=1)
    async def boom(messages):
        raise RuntimeError("boom")
    monkeypatch.setattr(gen, "_call", boom)
    res = await gen.generate(_make_ctx())
    assert res.provider == "fake(fallback)"
    assert any("回退" in n for n in res.risk_notes)


@pytest.mark.asyncio
async def test_llm_falls_back_on_garbage(monkeypatch):
    """Non-diff text triggers retries then falls back to Fake."""
    gen = LLMPatchGenerator(api_key="test-key", max_retries=1)
    async def garbage(messages):
        return "Sorry, I cannot do that."
    monkeypatch.setattr(gen, "_call", garbage)
    res = await gen.generate(_make_ctx())
    assert res.provider == "fake(fallback)"
    assert any("回退" in n for n in res.risk_notes)


def test_extract_diff_strips_fence():
    fenced = "```diff\n" + _VALID_DIFF + "\n```"
    assert _extract_diff(fenced).rstrip() == _VALID_DIFF.rstrip()


def test_extract_diff_rejects_non_diff():
    assert _extract_diff("just some text") == ""
    assert _extract_diff("--- a/x\n+++ b/x\n@@ -1 +1 @@\n unchanged\n") == ""


def test_extract_diff_is_newline_terminated():
    """Regression guard: a unified diff MUST end with "\\n".

    `git apply` rejects a diff whose last line is not newline-terminated with
    `corrupt patch at <last line>`. LLMs emit text without a trailing newline and
    `_extract_diff` strips whatever is there, so the terminator has to be restored.
    """
    assert _extract_diff(_VALID_DIFF.rstrip("\n")).endswith("\n")
    assert _extract_diff(_VALID_DIFF + "\n\n\n").endswith("\n")
    # exactly one trailing newline, not several
    assert not _extract_diff(_VALID_DIFF + "\n\n\n").endswith("\n\n")


def test_extract_diff_output_is_git_applicable(tmp_path):
    """End-to-end guard: the extracted diff must survive `git apply --check`.

    Root cause of a real production bug: `validate_patch` reported the patch
    `valid`, but the apply step failed with `corrupt patch at ...` because the
    stored diff was missing its trailing newline. This test would have caught it.
    """
    if shutil.which("git") is None:
        pytest.skip("git not available")

    target = tmp_path / "app.py"
    target.write_text("def normalize_email(e):\n    return e.strip()\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path, check=True,
    )

    # Note there is deliberately NO trailing newline here, exactly like LLM output.
    raw_llm_output = (
        "```diff\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def normalize_email(e):\n"
        "+    e = e.replace('\\uff20', '@')\n"
        "     return e.strip()\n"
        "```"
    )
    diff = _extract_diff(raw_llm_output)
    patch_file = tmp_path / "p.patch"
    patch_file.write_text(diff, encoding="utf-8", newline="\n")

    r = subprocess.run(
        ["git", "apply", "--check", str(patch_file)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert r.returncode == 0, f"git apply --check failed: {r.stderr}"


@pytest.mark.asyncio
async def test_module_generator_is_fake_without_key(monkeypatch):
    """When no DEEPSEEK_API_KEY is set, the module default generator is Fake."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("CODEFORGE_GENERATOR", "auto")
    # Re-import to re-run module init with patched env
    import importlib
    importlib.reload(patch_generator)
    assert isinstance(patch_generator._generator, patch_generator.FakePatchGenerator)


def test_llm_timeout_defaults_to_60(monkeypatch):
    """默认 60s —— 历史轮次的耗时口径靠它保持可比，不许改默认值。"""
    monkeypatch.delenv("CODEFORGE_LLM_TIMEOUT", raising=False)
    assert LLMPatchGenerator(api_key="k").timeout == 60.0


def test_llm_timeout_env_override(monkeypatch):
    """换强推理模型时用 env 放宽（2026-10-23 §5.4.3：v4-pro 单次 245s 被 60s 静默误杀）。"""
    monkeypatch.setenv("CODEFORGE_LLM_TIMEOUT", "600")
    assert LLMPatchGenerator(api_key="k").timeout == 600.0


def test_llm_timeout_explicit_arg_wins(monkeypatch):
    """显式入参优先于 env（测试与调用方可以精确控制）。"""
    monkeypatch.setenv("CODEFORGE_LLM_TIMEOUT", "600")
    assert LLMPatchGenerator(api_key="k", timeout=30.0).timeout == 30.0
