"""Pydantic contracts for VeriForge Phase 1."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

class RepositoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    local_path: str = Field(..., min_length=1, max_length=1000)
    default_branch: str = Field('main', min_length=1, max_length=120)
    allowed_source_roots: list[str] = Field(..., max_length=20)
    allowed_test_commands: list[str] = Field(default_factory=list, max_length=20)

class RepositoryResponse(RepositoryCreate):
    id: int
    created_at: str
    updated_at: str

class RepositoryStatusResponse(BaseModel):
    local_path: str
    git_root: str
    current_branch: str | None
    default_branch: str | None
    is_dirty: bool
    allowed_source_roots: list[str]

class CodeSearchRequest(BaseModel):
    query: str = Field('', max_length=200)
    filename: str | None = Field(None, max_length=200)
    symbol: str | None = Field(None, max_length=200)
    max_results: int = Field(50, ge=1, le=100)
    context_lines: int = Field(2, ge=0, le=10)

class CodeSearchResult(BaseModel):
    path: str
    line_start: int
    line_end: int
    matched_line: int
    symbol: str | None
    snippet: str
    match_type: str

class CodeReadRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=500)
    start_line: int = Field(1, ge=1)
    end_line: int | None = Field(None, ge=1)
    symbol: str | None = Field(None, max_length=200)

class CodeReadResponse(BaseModel):
    path: str
    line_start: int
    line_end: int
    content: str

class CodeTaskCreate(BaseModel):
    repository_id: int = Field(..., gt=0)
    issue_title: str = Field(..., min_length=1, max_length=200)
    issue_body: str = Field(..., min_length=1, max_length=10000)
    template_id: int | None = Field(None, gt=0)

class CodeTaskResponse(BaseModel):
    id: int
    repository_id: int
    issue_title: str
    issue_body: str
    status: str
    current_agent: str | None = None
    attempt_number: int
    error_message: str | None = None
    plan_decision: str | None = None
    reviewer_feedback: str | None = None
    template_id: int | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None

class TemplateParams(BaseModel):
    """五步链路的可调参数。

    默认值逐项等于改造前散落在 agent_graph / patch_service / test_runner 里的硬编码字面量，
    因此「不带模板」与「带一个空模板」都应当与改造前的行为完全一致。

    安全约束（模板只能收窄，不能放宽）：max_changed_lines ≤ 50、test_timeout_seconds ≤ 300、
    max_proposed_changes ≤ 5 是硬上限，越界直接 422，不做静默截断。
    max_keywords ≤ 12、max_acceptance_criteria ≤ 10 与 RequirementAnalysis 的字段上限对齐（领域契约不放宽）。
    """
    model_config = ConfigDict(extra='forbid')

    # ① 需求分析
    task_type: Literal['bug_fix', 'function_optimization', 'validation', 'unknown'] | None = None
    keywords: list[str] = Field(default_factory=list, max_length=12)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=10)
    risk_keywords: list[str] = Field(default_factory=list, max_length=20)
    max_keywords: int = Field(12, ge=1, le=12)
    max_acceptance_criteria: int = Field(10, ge=1, le=10)

    # ② 代码检索
    anchor_paths: list[str] = Field(default_factory=list, max_length=10)
    anchor_symbols: list[str] = Field(default_factory=list, max_length=10)
    search_aliases: dict[str, str] = Field(default_factory=dict, max_length=40)
    search_keyword_budget: int = Field(8, ge=1, le=20)
    per_keyword_max_results: int = Field(4, ge=1, le=20)
    search_result_limit: int = Field(8, ge=1, le=50)

    # ③ 方案生成
    max_proposed_changes: int = Field(5, ge=1, le=5)
    test_plan: list[str] = Field(default_factory=list, max_length=5)

    # ④ Patch 生成
    patch_provider: Literal['auto', 'fake', 'deepseek'] = 'auto'
    patch_model: str | None = Field(None, max_length=120)
    patch_max_retries: int = Field(2, ge=0, le=5)
    max_changed_lines: int = Field(50, ge=1, le=50)

    # ⑤ 测试审查
    test_command: str | None = Field(None, max_length=1000)
    test_timeout_seconds: int = Field(300, ge=1, le=300)

class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field('', max_length=500)
    repository_id: int | None = Field(None, gt=0)
    issue_pattern: str = Field('', max_length=200)
    params: TemplateParams = Field(default_factory=TemplateParams)

class TemplateResponse(BaseModel):
    id: int
    name: str
    description: str
    repository_id: int | None
    issue_pattern: str
    params: TemplateParams
    source_task_id: int | None
    use_count: int
    last_used_at: str | None
    created_at: str
    updated_at: str

class PlanDecisionRequest(BaseModel):
    reason: str | None = Field(None, max_length=2000)
class ReanalysisRequest(BaseModel):
    feedback: str = Field(..., min_length=1, max_length=2000)

class PatchValidateRequest(BaseModel):
    diff: str = Field(..., min_length=1, max_length=100000)

class PatchValidateResponse(BaseModel):
    id: int
    task_id: int
    patch_revision: int
    diff: str
    changed_files: list[str]
    changed_lines: int
    risk_level: str
    security_findings: list[str]
    apply_status: str
    approved: bool
    validation_status: str
    plan_id: int
    base_commit: str | None
    decision_status: str

class PatchDecisionRequest(BaseModel):
    reason: str | None = Field(None, max_length=2000)

class TestRunResponse(BaseModel):
    id: int
    task_id: int
    patch_id: int
    command: str
    status: str
    exit_code: int | None
    output: str
    duration_ms: int | None
    timed_out: bool
    environment_error: str | None

class TestRunRequest(BaseModel):
    """command 可省略：省略时回退到任务模板的 test_command（仍必须命中仓库白名单）。"""
    command: str | None = Field(None, min_length=1, max_length=1000)

class TaskEventResponse(BaseModel):
    event_seq: int
    event_type: str
    agent_name: str | None = None
    message: str | None = None
    payload: dict
    created_at: str

class ProposedChange(BaseModel):
    path: str
    description: str

class CodeContext(BaseModel):
    path: str
    line_start: int
    line_end: int
    matched_line: int | None = None
    snippet: str

class PatchGenerationContext(BaseModel):
    task_id: int
    repository_id: int
    plan_id: int
    issue_title: str = ''
    issue_body: str = ''
    plan_summary: str
    proposed_changes: list[ProposedChange]
    code_context: list[CodeContext]
    base_commit: str | None
    patch_revision: int
    reviewer_feedback: str | None
    # 仓库根目录：有值时生成器会在返回前试打一次补丁（生成期自检），
    # 不可应用就把 git 报错 + 失败位置的真实文件内容回喂重试。
    # 留空则完全跳过自检（测试与 Fake 生成器不受影响）。
    git_root: str = ''

class PatchGenerationResult(BaseModel):
    diff: str
    rationale: str
    changed_files: list[str]
    risk_notes: list[str]
    provider: str
    model: str | None = None
    prompt_digest: str | None = None
    response_digest: str | None = None
