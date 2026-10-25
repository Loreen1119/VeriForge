"""SQLite persistence for the VeriForge domain."""
from pathlib import Path
import os
import aiosqlite
from constants import (
    REQUIRED_TASK_STATUSES, REQUIRED_PLAN_STATUSES,
    REQUIRED_PATCH_APPLY_STATUSES, REQUIRED_PATCH_DECISION_STATUSES,
    REQUIRED_TEST_RUN_STATUSES,
    PLAN_DRAFT, PLAN_DECISION_APPROVED, PLAN_DECISION_REJECTED,
    PATCH_NOT_APPLIED, PATCH_DECISION_PENDING
)

DB_PATH = Path(__file__).resolve().parent / "codeforge.db"

# Generate CHECK constraint for task statuses
_TASK_STATUS_CHECK = ','.join(f"'{s}'" for s in sorted(REQUIRED_TASK_STATUSES))
_PLAN_STATUS_CHECK = ','.join(f"'{s}'" for s in sorted(REQUIRED_PLAN_STATUSES))
_PATCH_APPLY_STATUS_CHECK = ','.join(f"'{s}'" for s in sorted(REQUIRED_PATCH_APPLY_STATUSES))
_PATCH_DECISION_STATUS_CHECK = ','.join(f"'{s}'" for s in sorted(REQUIRED_PATCH_DECISION_STATUSES))
_TEST_RUN_STATUS_CHECK = ','.join(f"'{s}'" for s in sorted(REQUIRED_TEST_RUN_STATUSES))
_PLAN_DECISION_CHECK = f"'{PLAN_DECISION_APPROVED}','{PLAN_DECISION_REJECTED}'"

# Single source DDL definitions
CODE_TASKS_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS code_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    issue_title TEXT NOT NULL,
    issue_body TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ({_TASK_STATUS_CHECK})),
    current_agent TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    plan_decision TEXT CHECK(plan_decision IN ({_PLAN_DECISION_CHECK})),
    reviewer_feedback TEXT,
    next_event_seq INTEGER NOT NULL DEFAULT 1,
    template_id INTEGER,
    template_params_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT
)
"""

# 任务模板：把跑通的链路参数化后落库，同类 Issue 下次直接取用。
# template_id / source_task_id 刻意不加 REFERENCES 约束：它们是溯源指针，不是强关系。
# 加外键会在"删除模板"和"v1 迁移重建 code_tasks"两处引入耦合，而真正的可复现依据是
# code_tasks.template_params_json 快照列 —— 模板被改被删都不影响历史任务。
TASK_TEMPLATES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS task_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    repository_id INTEGER,
    issue_pattern TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    source_task_id INTEGER,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

REPOSITORIES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    local_path TEXT NOT NULL UNIQUE,
    default_branch TEXT NOT NULL DEFAULT 'main',
    allowed_source_roots TEXT NOT NULL DEFAULT '[]',
    allowed_test_commands TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

TASK_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES code_tasks(id) ON DELETE CASCADE,
    event_seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    agent_name TEXT,
    message TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, event_seq)
)
"""

PLANS_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES code_tasks(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ({_PLAN_STATUS_CHECK})),
    plan_json TEXT NOT NULL,
    review_json TEXT NOT NULL DEFAULT '{{}}',
    auto_review_passed INTEGER NOT NULL DEFAULT 0,
    override_reason TEXT,
    reviewer_id TEXT,
    reviewed_at TEXT,
    base_commit TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

PATCHES_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS patches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES code_tasks(id) ON DELETE CASCADE,
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    patch_revision INTEGER NOT NULL DEFAULT 1,
    base_commit TEXT,
    diff TEXT NOT NULL,
    changed_files TEXT NOT NULL DEFAULT '[]',
    changed_lines INTEGER NOT NULL DEFAULT 0,
    risk_level TEXT,
    security_findings TEXT NOT NULL DEFAULT '[]',
    validation_status TEXT NOT NULL DEFAULT 'valid' CHECK(validation_status IN ('valid','invalid')),
    apply_status TEXT NOT NULL DEFAULT 'not_applied' CHECK(apply_status IN ({_PATCH_APPLY_STATUS_CHECK})),
    apply_failure_reason TEXT,
    decision_status TEXT NOT NULL DEFAULT 'pending' CHECK(decision_status IN ({_PATCH_DECISION_STATUS_CHECK})),
    approved INTEGER NOT NULL DEFAULT 0,
    rejection_reason TEXT,
    UNIQUE(plan_id, patch_revision)
)
"""

TEST_RUNS_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES code_tasks(id) ON DELETE CASCADE,
    patch_id INTEGER REFERENCES patches(id),
    command TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ({_TEST_RUN_STATUS_CHECK})),
    exit_code INTEGER,
    output TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER,
    timed_out INTEGER NOT NULL DEFAULT 0,
    environment_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL = f"""
PRAGMA foreign_keys = ON;
{REPOSITORIES_TABLE_SQL};
{TASK_TEMPLATES_TABLE_SQL};
{CODE_TASKS_TABLE_SQL};
CREATE INDEX IF NOT EXISTS idx_code_tasks_created ON code_tasks(created_at DESC);
{TASK_EVENTS_TABLE_SQL};
{PLANS_TABLE_SQL};
{PATCHES_TABLE_SQL};
{TEST_RUNS_TABLE_SQL};
CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER PRIMARY KEY DEFAULT 1);
"""
async def _conn():
    conn = await aiosqlite.connect(str(DB_PATH)); conn.row_factory = aiosqlite.Row; return conn

async def _rebuild_table(conn: aiosqlite.Connection, old_name: str, new_name: str,
                         create_ddl: str, column_mapping: dict[str, str | None]) -> None:
    """Rebuild a table by renaming, creating new, copying data, dropping old.

    Args:
        old_name: Current table name
        new_name: Name for new table
        create_ddl: DDL to create new table (with new_name)
        column_mapping: {new_col: old_col_or_None} - None means use default/NULL
    """
    await conn.execute("PRAGMA foreign_keys = OFF")
    temp_old = f"_{old_name}_tmp"
    await conn.execute(f"ALTER TABLE {old_name} RENAME TO {temp_old}")
    await conn.execute(create_ddl)

    new_cols = ', '.join(column_mapping.keys())
    old_cols = ', '.join(
        f"{col} AS {new_col}" if col else f"NULL AS {new_col}"
        for new_col, col in column_mapping.items()
    )
    await conn.execute(f"INSERT INTO {new_name} ({new_cols}) SELECT {old_cols} FROM {temp_old}")
    await conn.execute(f"DROP TABLE {temp_old}")
    await conn.execute("PRAGMA foreign_keys = ON")

async def _migrate_v1_code_tasks_status_check(conn: aiosqlite.Connection) -> None:
    """Migration v1: Expand code_tasks status CHECK to include all 16 states."""
    rows = await (await conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='code_tasks'"
    )).fetchall()
    if not rows:
        return
    create_sql = rows[0][0] or ''
    if "test_timeout" in create_sql:
        return  # Already has full status list

    await _rebuild_table(
        conn, 'code_tasks', 'code_tasks', CODE_TASKS_TABLE_SQL,
        {
            'id': 'id',
            'repository_id': 'repository_id',
            'issue_title': 'issue_title',
            'issue_body': 'issue_body',
            'status': 'status',
            'current_agent': 'current_agent',
            'attempt_number': 'attempt_number',
            'error_message': 'error_message',
            'plan_decision': 'plan_decision',
            'reviewer_feedback': 'reviewer_feedback',
            'next_event_seq': 'next_event_seq',
            'created_at': 'created_at',
            'started_at': 'started_at',
            'finished_at': 'finished_at',
        }
    )

async def _migrate_v2_patches_table_rebuild(conn: aiosqlite.Connection) -> None:
    """Migration v2: Rebuild patches table - make plan_id NOT NULL, add patch_revision,
    add UNIQUE(plan_id, patch_revision), add apply_failure_reason, improve apply_status CHECK."""
    # Check if already migrated
    rows = await (await conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='patches'"
    )).fetchall()
    if not rows:
        return
    create_sql = rows[0][0] or ''
    if "patch_revision" in create_sql:
        return  # Already migrated

    # First: backfill plan_id for rows that don't have it
    # Find the latest plan for each task
    await conn.execute("""
UPDATE patches
SET plan_id = (
    SELECT id FROM plans
    WHERE plans.task_id = patches.task_id
    ORDER BY id DESC
    LIMIT 1
)
WHERE plan_id IS NULL
""")

    # Delete any rows where backfill failed (no plan found)
    await conn.execute("DELETE FROM patches WHERE plan_id IS NULL")

    # Now rebuild the table
    await _rebuild_table(
        conn, 'patches', 'patches', PATCHES_TABLE_SQL,
        {
            'id': 'id',
            'task_id': 'task_id',
            'plan_id': 'plan_id',
            'patch_revision': None,  # DEFAULT 1
            'base_commit': 'base_commit',
            'diff': 'diff',
            'changed_files': 'changed_files',
            'changed_lines': 'changed_lines',
            'risk_level': 'risk_level',
            'security_findings': 'security_findings',
            'validation_status': 'validation_status',
            'apply_status': 'apply_status',
            'apply_failure_reason': None,  # New field
            'decision_status': 'decision_status',
            'approved': 'approved',
            'rejection_reason': 'rejection_reason',
        }
    )

async def _migrate_v3_test_runs_status_check(conn: aiosqlite.Connection) -> None:
    """Migration v3: Add test_runs status CHECK with all valid values including 'interrupted'."""
    rows = await (await conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='test_runs'"
    )).fetchall()
    if not rows:
        return
    create_sql = rows[0][0] or ''
    if "interrupted" in create_sql:
        return  # Already has the new status CHECK

    # Rebuild test_runs table to add CHECK
    await _rebuild_table(
        conn, 'test_runs', 'test_runs', TEST_RUNS_TABLE_SQL,
        {
            'id': 'id',
            'task_id': 'task_id',
            'patch_id': 'patch_id',
            'command': 'command',
            'status': 'status',
            'exit_code': 'exit_code',
            'output': 'output',
            'duration_ms': 'duration_ms',
            'timed_out': 'timed_out',
            'environment_error': 'environment_error',
            'created_at': 'created_at',
        }
    )

async def _migrate_v4_code_tasks_template_columns(conn: aiosqlite.Connection) -> None:
    """Migration v4: 给 code_tasks 加模板关联列与参数快照列。

    用 ALTER TABLE ADD COLUMN，不用 _rebuild_table：code_tasks 是父表，被
    task_events / plans / patches / test_runs 四张 ON DELETE CASCADE 表引用，
    重建的爆炸半径明显大于加两列。两列都可空，对已有行无影响。
    """
    rows = await (await conn.execute("PRAGMA table_info(code_tasks)")).fetchall()
    if not rows:
        return  # 表不存在，DDL 会补齐
    existing = {row[1] for row in rows}
    if "template_id" not in existing:
        await conn.execute("ALTER TABLE code_tasks ADD COLUMN template_id INTEGER")
    if "template_params_json" not in existing:
        await conn.execute("ALTER TABLE code_tasks ADD COLUMN template_params_json TEXT")

# Ordered list of migrations
MIGRATIONS = [
    _migrate_v1_code_tasks_status_check,
    _migrate_v2_patches_table_rebuild,
    _migrate_v3_test_runs_status_check,
    _migrate_v4_code_tasks_template_columns,
]

async def _get_schema_version(conn: aiosqlite.Connection) -> int:
    """Get current schema version from schema_meta table."""
    try:
        row = await (await conn.execute("SELECT version FROM schema_meta LIMIT 1")).fetchone()
        return row[0] if row else 0
    except:
        return 0

async def _set_schema_version(conn: aiosqlite.Connection, version: int) -> None:
    """Update schema version."""
    await conn.execute("DELETE FROM schema_meta")
    await conn.execute("INSERT INTO schema_meta(version) VALUES(?)", (version,))

async def init_db() -> None:
    os.makedirs(DB_PATH.parent, exist_ok=True)
    conn = await _conn()
    try:
        await conn.executescript(_DDL)

        # Initialize schema_meta if not exists
        try:
            await conn.execute("SELECT 1 FROM schema_meta LIMIT 1")
        except:
            await conn.execute("INSERT INTO schema_meta(version) VALUES(0)")
            await conn.commit()

        # Run pending migrations
        current_version = await _get_schema_version(conn)
        for i, migration in enumerate(MIGRATIONS):
            if i >= current_version:
                await migration(conn)
                await conn.commit()
        await _set_schema_version(conn, len(MIGRATIONS))
        await conn.commit()
    finally:
        await conn.close()

