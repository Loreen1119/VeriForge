"""VeriForge Phase 1 API: repositories, code tasks, durable events and SSE replay."""
import asyncio, json, os, sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from db import init_db
from schemas import RepositoryCreate, RepositoryResponse, RepositoryStatusResponse, CodeSearchRequest, CodeSearchResult, CodeReadRequest, CodeReadResponse, CodeTaskCreate, CodeTaskResponse, TaskEventResponse, PlanDecisionRequest, ReanalysisRequest, PatchValidateRequest, PatchValidateResponse, PatchDecisionRequest, TestRunResponse, TestRunRequest, PatchGenerationContext, CodeContext, ProposedChange, TemplateCreate, TemplateResponse
from repository_service import RepositoryValidationError, inspect_repository
from code_search import read_code, search_repository
from patch_service import validate_patch
from task_service import create_repository,list_repositories,get_repository,create_task_record,get_task,list_tasks,update_task,request_reanalysis,supersede_for_reanalysis,create_plan,create_patch,get_current_plan,get_plan,get_patch,update_patch,decide_plan,append_event,list_events,schedule_task,shutdown_unfinished_tasks,create_test_run,list_test_runs,transition_task,schedule_generation,validate_and_create_patch,recover_zombie_tasks,load_template_params,effective_max_changed_lines
from worktree_service import create_worktree, apply_patch, remove_worktree, worktree_path, WorktreeError
from test_runner import syntax_check, run_allowlisted
from constants import SSE_CLOSE_STATES, AWAITING_PLAN_APPROVAL, NEEDS_CONTEXT, PENDING, GENERATING_PATCH, AWAITING_PATCH_GENERATION_RETRY, AWAITING_PATCH_APPROVAL, RUNNING, CANCELLED, PATCH_APPLY_FAILED, APPLY_FAILURE_WORKTREE_CREATE_FAILED, APPLY_FAILURE_GIT_APPLY_FAILED, APPLY_FAILURE_SYNTAX_FAILED, APPLY_FAILURE_BASE_COMMIT_CHANGED
from patch_generator import generate_patch
from template_service import TemplateBoundaryError, create_template, delete_template, get_template, list_templates, mark_template_used, parse_params, resolve_params

def _now(): return datetime.now(timezone.utc).isoformat()
@asynccontextmanager
async def lifespan(app):
    await init_db()
    await recover_zombie_tasks()
    yield
    await shutdown_unfinished_tasks()
api=FastAPI(title='VeriForge API',version='0.1.0',description='可追踪、可审查的代码维护任务平台',lifespan=lifespan)
@api.get('/health')
async def health(): return {'status':'ok','service':'veriforge'}
def _repo(row):
    d=dict(row); d['allowed_source_roots']=json.loads(d['allowed_source_roots']); d['allowed_test_commands']=json.loads(d['allowed_test_commands']); return d
@api.post('/api/repositories',response_model=RepositoryResponse,status_code=201)
async def add_repository(req:RepositoryCreate):
    try:
        inspect_repository(req.local_path, req.allowed_source_roots)
        rid=await create_repository(req.model_dump())
    except (RepositoryValidationError, OSError, ValueError, sqlite3.IntegrityError) as exc: raise HTTPException(400,'仓库配置无效、不是 Git 仓库或路径已存在') from exc
    return _repo(await get_repository(rid))
@api.get('/api/repositories',response_model=list[RepositoryResponse])
async def repositories(): return [_repo(r) for r in await list_repositories()]
@api.get('/api/repositories/{repository_id}/status',response_model=RepositoryStatusResponse)
async def repository_status(repository_id:int):
    row=await get_repository(repository_id)
    if row is None: raise HTTPException(404,'仓库不存在')
    try: return inspect_repository(row['local_path'], json.loads(row['allowed_source_roots']))
    except RepositoryValidationError as exc: raise HTTPException(400,str(exc)) from exc
@api.post('/api/repositories/{repository_id}/search',response_model=list[CodeSearchResult])
async def search_code(repository_id:int, req:CodeSearchRequest):
    row=await get_repository(repository_id)
    if row is None: raise HTTPException(404,'仓库不存在')
    try:
        info=inspect_repository(row['local_path'], json.loads(row['allowed_source_roots']))
        return search_repository(info['git_root'], req.query, filename=req.filename, symbol=req.symbol, allowed_source_roots=json.loads(row['allowed_source_roots']), max_results=req.max_results, context_lines=req.context_lines)
    except (RepositoryValidationError, ValueError) as exc: raise HTTPException(400,str(exc)) from exc
@api.post('/api/repositories/{repository_id}/read',response_model=CodeReadResponse)
async def read_source(repository_id:int, req:CodeReadRequest):
    row=await get_repository(repository_id)
    if row is None: raise HTTPException(404,'仓库不存在')
    try:
        info=inspect_repository(row['local_path'], json.loads(row['allowed_source_roots']))
        return read_code(info['git_root'], req.path, start_line=req.start_line, end_line=req.end_line, symbol=req.symbol, allowed_source_roots=json.loads(row['allowed_source_roots']))
    except (RepositoryValidationError, ValueError) as exc: raise HTTPException(400,str(exc)) from exc
# ---------- 任务模板：把跑通的五步链路参数化后落库复用 ----------
def _template_response(template: dict) -> dict:
    """TemplateResponse 的 params 是结构化对象；库里存的是 JSON，这里转一层。"""
    return {**template, 'params': template['params'].model_dump()}

@api.post('/api/templates',response_model=TemplateResponse,status_code=201)
async def add_template(req:TemplateCreate):
    if req.repository_id is not None and await get_repository(req.repository_id) is None:
        raise HTTPException(404,'模板绑定的仓库不存在')
    try:
        template_id=await create_template(name=req.name,params=req.params,description=req.description,
                                         repository_id=req.repository_id,issue_pattern=req.issue_pattern)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(400,'模板名称已存在') from exc
    return _template_response(await get_template(template_id))
@api.get('/api/templates',response_model=list[TemplateResponse])
async def templates(): return [_template_response(item) for item in await list_templates()]
@api.post('/api/templates/from-task/{task_id}',response_model=TemplateResponse,status_code=201)
async def template_from_task(task_id:int,name:str|None=None):
    """把一次已跑通的任务沉淀成模板：直接取任务上的参数快照。

    改造前创建的任务没有 snapshot 列，回退到链路默认参数 —— 沉淀出的仍是可复用模板，
    只是等于当前默认值，不会凭空造出没生效过的参数。
    """
    row=await get_task(task_id)
    if row is None: raise HTTPException(404,'任务不存在')
    issue_title=row['issue_title']
    try:
        template_id=await create_template(
            name=(name or f'{issue_title[:40]} · 沉淀').strip(),
            params=parse_params(row['template_params_json']),
            description=f'由任务 #{task_id} 沉淀',
            issue_pattern=issue_title[:200],
            source_task_id=task_id
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(400,'模板名称已存在，请用 name 参数指定新名称') from exc
    return _template_response(await get_template(template_id))
@api.get('/api/templates/{template_id}',response_model=TemplateResponse)
async def template_detail(template_id:int):
    template=await get_template(template_id)
    if template is None: raise HTTPException(404,'模板不存在')
    return _template_response(template)
@api.delete('/api/templates/{template_id}',status_code=204)
async def remove_template(template_id:int):
    if not await delete_template(template_id): raise HTTPException(404,'模板不存在')
@api.post('/api/tasks',response_model=CodeTaskResponse,status_code=202)
async def add_task(req:CodeTaskCreate):
    repository=await get_repository(req.repository_id)
    if repository is None: raise HTTPException(404,'仓库不存在')
    template_id=None; template_name=None; params_json=None
    if req.template_id is not None:
        template=await get_template(req.template_id)
        if template is None: raise HTTPException(404,'模板不存在')
        try:
            # 模板参数在这里与目标仓库的安全边界收敛；越界直接 422，不静默降级。
            params=resolve_params(template,dict(repository),repository_id=req.repository_id)
        except TemplateBoundaryError as exc:
            raise HTTPException(422,str(exc)) from exc
        template_id=template['id']; template_name=template['name']
        params_json=json.dumps(params,ensure_ascii=False)
    tid=await create_task_record({**req.model_dump(),'template_params_json':params_json})
    await append_event(tid,'TASK_CREATED',{'type':'TASK_CREATED','status':'pending','template_id':template_id,'template_name':template_name},'代码任务已创建')
    if template_id is not None: await mark_template_used(template_id)
    await schedule_task(tid); return dict(await get_task(tid))
@api.get('/api/tasks',response_model=list[CodeTaskResponse])
async def tasks(): return [dict(r) for r in await list_tasks()]
@api.get('/api/tasks/{task_id}',response_model=CodeTaskResponse)
async def task(task_id:int):
    row=await get_task(task_id)
    if row is None: raise HTTPException(404,'任务不存在')
    return dict(row)
async def _plan_decision(task_id:int, decision:str, req:PlanDecisionRequest):
    row=await get_task(task_id)
    if row is None: raise HTTPException(404,'任务不存在')
    if decision == 'approve' and row['status'] == 'needs_context': raise HTTPException(409,'上下文不足，不能确认方案')
    if row['status'] not in (AWAITING_PLAN_APPROVAL, NEEDS_CONTEXT): raise HTTPException(409,'任务当前不在方案确认阶段')
    plan=await get_current_plan(task_id)
    if plan is None: raise HTTPException(409,'当前没有可确认的 Plan')
    reason=req.reason.strip() if req.reason else None
    if decision == 'reject' and not reason: raise HTTPException(422,'驳回方案必须填写理由')
    if decision == 'approve' and not plan['auto_review_passed'] and not reason: raise HTTPException(409,'自动审查未通过，必须填写 override_reason')
    if not await decide_plan(plan['id'],'approved' if decision == 'approve' else 'rejected',reason if decision == 'approve' and not plan['auto_review_passed'] else None): raise HTTPException(409,'Plan 状态已变化，请刷新后重试')

    if decision == 'approve':
        # Transition to generating_patch and schedule background generation
        await transition_task(
            task_id,
            to_status=GENERATING_PATCH,
            event_type='PLAN_APPROVED',
            payload={'type': 'PLAN_APPROVED', 'reason': reason},
            message='方案已通过人工确认，开始生成修复方案',
            agent_name='human_reviewer',
            plan_decision='approved'
        )
        # Schedule patch generation as background task
        await schedule_generation(task_id)
    else:
        # Reject: transition to needs_context
        await transition_task(
            task_id,
            to_status=NEEDS_CONTEXT,
            event_type='PLAN_REJECTED',
            payload={'type': 'PLAN_REJECTED', 'reason': reason},
            message='方案已驳回，等待补充理由后重新分析',
            agent_name='human_reviewer',
            plan_decision='rejected'
        )

    return dict(await get_task(task_id))
@api.post('/api/tasks/{task_id}/plan/approve',response_model=CodeTaskResponse)
async def approve_plan(task_id:int, req:PlanDecisionRequest=PlanDecisionRequest()):
    return await _plan_decision(task_id,'approve',req)
@api.post('/api/tasks/{task_id}/plan/reject',response_model=CodeTaskResponse)
async def reject_plan(task_id:int, req:PlanDecisionRequest):
    return await _plan_decision(task_id,'reject',req)
@api.post('/api/tasks/{task_id}/patches/regenerate',response_model=CodeTaskResponse,status_code=202)
async def regenerate_patch(task_id:int):
    row=await get_task(task_id)
    if row is None: raise HTTPException(404,'任务不存在')
    if row['status'] != AWAITING_PATCH_GENERATION_RETRY: raise HTTPException(409,'当前状态不允许重新生成补丁')
    if not await transition_task(
        task_id,
        to_status=GENERATING_PATCH,
        event_type='TASK_STARTED',
        from_statuses=[AWAITING_PATCH_GENERATION_RETRY],
        payload={'type':'TASK_STARTED','from_status':AWAITING_PATCH_GENERATION_RETRY},
        message='人工触发重新生成补丁',
        agent_name='human_reviewer',
    ): raise HTTPException(409,'状态已变化，请刷新后重试')
    await schedule_generation(task_id)
    return dict(await get_task(task_id))

@api.post('/api/tasks/{task_id}/plan/reanalyze',response_model=CodeTaskResponse,status_code=202)
async def reanalyze_plan(task_id:int, req:ReanalysisRequest):
    feedback=req.feedback.strip()
    if not feedback: raise HTTPException(422,'重新分析必须填写反馈')
    if await get_task(task_id) is None: raise HTTPException(404,'任务不存在')
    if not await request_reanalysis(task_id,feedback): raise HTTPException(409,'任务当前不允许重新分析')
    await append_event(task_id,'PLAN_REANALYSIS_REQUESTED',{'type':'PLAN_REANALYSIS_REQUESTED','feedback':feedback},'已收到人工反馈，开始重新分析','human_reviewer')
    await schedule_task(task_id)
    return dict(await get_task(task_id))
@api.post('/api/tasks/{task_id}/cancel',response_model=CodeTaskResponse,status_code=200)
async def cancel_task(task_id:int):
    row=await get_task(task_id)
    if row is None: raise HTTPException(404,'任务不存在')
    if row['status'] != RUNNING: raise HTTPException(409,'只能取消 running 状态的任务')
    if not await transition_task(
        task_id,
        to_status=CANCELLED,
        event_type='TASK_CANCELLED',
        from_statuses=[RUNNING],
        payload={'type':'TASK_CANCELLED'},
        message='任务已被用户取消',
        agent_name='human_reviewer',
        finished_at=_now()
    ): raise HTTPException(409,'状态已变化，请刷新后重试')
    return dict(await get_task(task_id))
@api.post('/api/tasks/{task_id}/patches/validate',response_model=PatchValidateResponse,status_code=201)
async def validate_task_patch(task_id:int, req:PatchValidateRequest):
    row=await get_task(task_id)
    if row is None: raise HTTPException(404,'任务不存在')
    if row['status'] not in (AWAITING_PATCH_APPROVAL,) or row['plan_decision'] != 'approved': raise HTTPException(409,'补丁生成完成后才能提交')
    repository=await get_repository(row['repository_id'])
    if repository is None: raise HTTPException(404,'仓库不存在')
    roots=json.loads(repository['allowed_source_roots'])
    try:
        info=inspect_repository(repository['local_path'],roots)
        plan=await get_current_plan(task_id)
        if plan is None or plan['status'] != 'approved': raise HTTPException(409,'当前任务没有已确认 Plan')
        plan_json=json.loads(plan['plan_json'])
        # 模板的 max_changed_lines 也必须在这条手动提交路径上生效，否则模板收窄的
        # 行数限制可以被绕过（自动生成走模板、手动提交走默认 50）。
        params=load_template_params(row)
        patch_id, check, valid = await validate_and_create_patch(task_id, row['attempt_number'], req.diff, plan['id'], plan['base_commit'], plan_json, info['git_root'], roots, max_changed_lines=effective_max_changed_lines(params))
        await append_event(task_id,'PATCH_VALIDATED',{'type':'PATCH_VALIDATED','patch_id':patch_id,'check':check.model_dump()},'Patch 安全检查完成，等待人工确认','patch_agent')
        saved=await get_patch(patch_id)
        if not valid:
            raise HTTPException(status_code=422, detail={'message':'Patch 校验失败','patch_id':patch_id,'findings':check.findings})
        return _patch_response(saved, valid)
    except (RepositoryValidationError, ValueError) as exc: raise HTTPException(400,str(exc)) from exc
def _patch_response(row, valid=None):
    validation_status = row['validation_status'] if 'validation_status' in row.keys() else ('invalid' if row['decision_status'] == 'invalid' else 'valid')
    if valid is not None:
        validation_status = 'valid' if valid else 'invalid'
    return {'id':row['id'],'task_id':row['task_id'],'patch_revision':row['patch_revision'],'diff':row['diff'],'changed_files':json.loads(row['changed_files']),'changed_lines':row['changed_lines'],'risk_level':row['risk_level'],'security_findings':json.loads(row['security_findings']),'apply_status':row['apply_status'],'approved':bool(row['approved']),'validation_status':validation_status,'plan_id':row['plan_id'],'base_commit':row['base_commit'],'decision_status':row['decision_status']}
@api.get('/api/tasks/{task_id}/patches/{patch_id}',response_model=PatchValidateResponse)
async def get_task_patch(task_id:int, patch_id:int):
    row=await get_patch(patch_id)
    if row is None or row['task_id'] != task_id: raise HTTPException(404,'Patch 不存在')
    return _patch_response(row)
async def _patch_decision(task_id:int, patch_id:int, decision:str, req:PatchDecisionRequest):
    row=await get_patch(patch_id)
    if row is None or row['task_id'] != task_id: raise HTTPException(404,'Patch 不存在')
    if row['decision_status'] != 'pending': raise HTTPException(409,'Patch 当前状态不允许人工确认')
    if row['validation_status'] != 'valid' or row['security_findings'] != '[]' or row['apply_status'] != 'not_applied': raise HTTPException(409,'Patch 未通过安全校验')
    if decision == 'reject' and not req.reason: raise HTTPException(422,'驳回 Patch 必须填写理由')
    approved=decision == 'approve'
    await update_patch(patch_id,approved=1 if approved else 0,decision_status='approved' if approved else 'rejected',rejection_reason=req.reason if not approved else None)
    event='PATCH_APPROVED' if approved else 'PATCH_REJECTED'
    await append_event(task_id,event,{'type':event,'patch_id':patch_id,'reason':req.reason},'Patch 已通过人工确认' if approved else 'Patch 已驳回','human_reviewer')
    return _patch_response(await get_patch(patch_id))
@api.post('/api/tasks/{task_id}/patches/{patch_id}/approve',response_model=PatchValidateResponse)
async def approve_patch(task_id:int, patch_id:int, req:PatchDecisionRequest=PatchDecisionRequest()):
    return await _patch_decision(task_id,patch_id,'approve',req)
@api.post('/api/tasks/{task_id}/patches/{patch_id}/reject',response_model=PatchValidateResponse)
async def reject_patch(task_id:int, patch_id:int, req:PatchDecisionRequest):
    return await _patch_decision(task_id,patch_id,'reject',req)

@api.post('/api/tasks/{task_id}/patches/{patch_id}/apply', response_model=PatchValidateResponse)
async def apply_task_patch(task_id:int, patch_id:int):
    row=await get_patch(patch_id)
    if row is None or row['task_id'] != task_id: raise HTTPException(404,'Patch 不存在')
    if row['decision_status'] != 'approved': raise HTTPException(409,'Patch 尚未通过人工确认')
    repo=await get_repository((await get_task(task_id))['repository_id'])

    async def _fail(reason:str, detail:str, message:str, status:int):
        """失败必须留痕：写 apply_status / apply_failure_reason，并落一条 PATCH_APPLY_FAILED 事件。"""
        await update_patch(patch_id, apply_status=PATCH_APPLY_FAILED, apply_failure_reason=reason)
        await append_event(task_id,'PATCH_APPLY_FAILED',
            {'type':'PATCH_APPLY_FAILED','patch_id':patch_id,'reason':reason,'detail':detail},
            message,'worktree')
        raise HTTPException(status, detail or message)

    git_root=None
    try:
        info=inspect_repository(repo['local_path'], json.loads(repo['allowed_source_roots']))
        git_root=info['git_root']
        if row['base_commit'] and info.get('base_commit') != row['base_commit']:
            await _fail(APPLY_FAILURE_BASE_COMMIT_CHANGED,'仓库基准提交已变化','仓库基准提交已变化',409)
        wt=create_worktree(git_root, task_id, row['base_commit'], reuse=True)
        changed=json.loads(row['changed_files'])
        mode=apply_patch(wt,row['diff'])
        ok, output=syntax_check(wt,changed)
        if not ok:
            await _fail(APPLY_FAILURE_SYNTAX_FAILED, output, 'Patch 语法检查失败', 422)
        await update_patch(patch_id,apply_status='applied',apply_failure_reason=None)
        await append_event(task_id,'PATCH_APPLIED',
            {'type':'PATCH_APPLIED','patch_id':patch_id,'apply_mode':mode},
            'Patch 已在隔离 Worktree 应用','worktree')
        return _patch_response(await get_patch(patch_id))
    except WorktreeError as exc:
        if git_root is not None: remove_worktree(git_root, task_id)
        reason=(APPLY_FAILURE_WORKTREE_CREATE_FAILED if git_root is None
                else APPLY_FAILURE_GIT_APPLY_FAILED)
        # apply_patch 会把四档降级链的 git 原始 stderr 附在异常第二个参数里，
        # 这里原样落进 detail —— 前端 / 后续「按 git 报错重试」能拿到真正的失败原因
        # （例如 `patch failed: src/x.py:1`），而不是只有一句笼统的「Patch 应用失败」。
        await _fail(reason, str(exc), 'Patch 应用失败', 422)

@api.post('/api/tasks/{task_id}/patches/{patch_id}/tests', response_model=TestRunResponse, status_code=201)
async def run_patch_test(task_id:int, patch_id:int, req:TestRunRequest):
    row=await get_patch(patch_id)
    if row is None or row['task_id'] != task_id: raise HTTPException(404,'Patch 不存在')
    task_row=await get_task(task_id); repo=await get_repository(task_row['repository_id'])
    params=load_template_params(task_row)
    # 命令可以由请求给出，也可以回落到模板的 test_command；两者都没有就明确拒绝，
    # 不做"空命令当作通过"之类的降级。
    command=req.command or params.get('test_command')
    if not command: raise HTTPException(422,'未指定测试命令，且当前任务没有可用的模板 test_command')
    allowed=json.loads(repo['allowed_test_commands'])
    if command not in allowed: raise HTTPException(403,'命令不在白名单中')
    wt=worktree_path(repo['local_path'],task_id)
    result=run_allowlisted(command,wt,allowed,timeout_seconds=params.get('test_timeout_seconds',300))
    rid=await create_test_run(task_id,patch_id,result,command)
    await append_event(task_id,'TEST_RUN_COMPLETED',{'type':'TEST_RUN_COMPLETED','test_run_id':rid,'status':result['status']},'白名单测试执行完成','test_runner')
    return {'id':rid,'task_id':task_id,'patch_id':patch_id,'command':command,**result,'timed_out':bool(result.get('timed_out'))}

@api.post('/api/tasks/{task_id}/patches/{patch_id}/tests/run', response_model=TestRunResponse, status_code=201)
async def run_patch_test_alias(task_id:int, patch_id:int, req:TestRunRequest):
    return await run_patch_test(task_id, patch_id, req)

@api.get('/api/tasks/{task_id}/test-runs', response_model=list[TestRunResponse])
async def task_test_runs(task_id:int):
    if await get_task(task_id) is None: raise HTTPException(404,'任务不存在')
    return [dict(r) | {'timed_out':bool(r['timed_out'])} for r in await list_test_runs(task_id)]
@api.get('/api/tasks/{task_id}/events',response_model=list[TaskEventResponse])
async def events(task_id:int):
    if await get_task(task_id) is None: raise HTTPException(404,'任务不存在')
    return [{'event_seq':s,'event_type':t,'agent_name':a,'message':m,'payload':p,'created_at':c} for s,t,a,m,p,c in await list_events(task_id)]
def _frame(payload,event_id=None):
    prefix=f'id: {event_id}\n' if event_id is not None else ''; return prefix+'data: '+json.dumps(payload,ensure_ascii=False)+'\n\n'
@api.get('/api/tasks/{task_id}/stream')
async def stream(task_id:int,request:Request):
    if await get_task(task_id) is None: raise HTTPException(404,'任务不存在')
    try: last=int(request.headers.get('Last-Event-ID','0') or 0)
    except ValueError: last=0
    async def body():
        nonlocal last
        while True:
            for seq,_,_,_,payload,_ in await list_events(task_id,last): last=seq; yield _frame(payload,seq)
            row=await get_task(task_id)
            if row is None or row['status'] in SSE_CLOSE_STATES: return
            if await request.is_disconnected(): return
            yield ': keepalive\n\n'; await asyncio.sleep(.2)
    return StreamingResponse(body(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
if __name__=='__main__':
    import uvicorn; uvicorn.run('main:api',host='127.0.0.1',port=int(os.getenv('PORT','8000')))
