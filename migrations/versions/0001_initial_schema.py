"""initial schema

Revision ID: 6f536324eafc
Revises: 
Create Date: 2026-08-13 20:15:31.653108
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from neuroloop.persistence.models import JsonType

revision: str = '0001_initial'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('agents',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('skills',
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.Column('version', sa.String(length=40), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('trigger_tags', JsonType, nullable=False),
    sa.Column('required_inputs', JsonType, nullable=False),
    sa.Column('preconditions', JsonType, nullable=False),
    sa.Column('action_template', JsonType, nullable=False),
    sa.Column('success_criteria', JsonType, nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('usage_count', sa.Integer(), nullable=False),
    sa.Column('success_count', sa.Integer(), nullable=False),
    sa.Column('disabled_reason', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id', 'version')
    )
    op.create_table('goals',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('agent_id', sa.Uuid(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('priority', sa.Float(), nullable=False),
    sa.Column('deadline', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('success_criteria', JsonType, nullable=False),
    sa.Column('failure_criteria', JsonType, nullable=False),
    sa.Column('constraints', JsonType, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.CheckConstraint('priority >= 0 AND priority <= 1', name='ck_goals_priority'),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('goals', schema=None) as batch_op:
        batch_op.create_index('ix_goals_agent_status', ['agent_id', 'status'], unique=False)
        batch_op.create_index(batch_op.f('ix_goals_status'), ['status'], unique=False)

    op.create_table('agent_runs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('goal_id', sa.Uuid(), nullable=False),
    sa.Column('phase', sa.String(length=20), nullable=False),
    sa.Column('iteration', sa.Integer(), nullable=False),
    sa.Column('active_plan_id', sa.Uuid(), nullable=True),
    sa.Column('active_plan_version', sa.Integer(), nullable=True),
    sa.Column('current_step_id', sa.String(length=100), nullable=True),
    sa.Column('replan_count', sa.Integer(), nullable=False),
    sa.Column('plan_generation_count', sa.Integer(), nullable=False),
    sa.Column('retry_counts', JsonType, nullable=False),
    sa.Column('waiting_reason', sa.String(length=100), nullable=True),
    sa.Column('pending_approval_action_id', sa.Uuid(), nullable=True),
    sa.Column('pending_approval_fingerprint', sa.String(length=80), nullable=True),
    sa.Column('last_action_id', sa.Uuid(), nullable=True),
    sa.Column('last_verified_action_id', sa.Uuid(), nullable=True),
    sa.Column('unresolved_effect_action_id', sa.Uuid(), nullable=True),
    sa.Column('budget', JsonType, nullable=False),
    sa.Column('tokens_used', sa.Integer(), nullable=False),
    sa.Column('cost_used_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('baseline_outcomes', JsonType, nullable=False),
    sa.Column('cancel_requested', sa.Boolean(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('wall_clock_deadline', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('state_version', sa.Integer(), nullable=False),
    sa.Column('lease_owner', sa.String(length=120), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lease_epoch', sa.Integer(), nullable=False),
    sa.Column('error_code', sa.String(length=40), nullable=True),
    sa.CheckConstraint('iteration >= 0', name='ck_runs_iteration'),
    sa.CheckConstraint('plan_generation_count >= replan_count', name='ck_runs_plan_counters'),
    sa.CheckConstraint('state_version >= 0', name='ck_runs_state_version'),
    sa.ForeignKeyConstraint(['goal_id'], ['goals.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('agent_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_agent_runs_goal_id'), ['goal_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_runs_phase'), ['phase'], unique=False)
        batch_op.create_index('ix_runs_lease', ['lease_expires_at'], unique=False)

    op.create_table('actions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('logical_action_id', sa.Uuid(), nullable=False),
    sa.Column('tool', sa.String(length=100), nullable=False),
    sa.Column('tool_version', sa.String(length=40), nullable=False),
    sa.Column('arguments', JsonType, nullable=False),
    sa.Column('expected_outcomes', JsonType, nullable=False),
    sa.Column('rationale_code', sa.String(length=80), nullable=False),
    sa.Column('plan_step_id', sa.String(length=100), nullable=True),
    sa.Column('idempotency_key', sa.String(length=80), nullable=False),
    sa.Column('action_fingerprint', sa.String(length=80), nullable=False),
    sa.Column('derived_from', JsonType, nullable=False),
    sa.Column('risk_level', sa.String(length=4), nullable=False),
    sa.Column('approved_by_user', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.create_index('ix_actions_fingerprint', ['run_id', 'action_fingerprint'], unique=False)
        batch_op.create_index(batch_op.f('ix_actions_logical_action_id'), ['logical_action_id'], unique=False)
        batch_op.create_index('uq_actions_idempotency', ['idempotency_key'], unique=True)

    op.create_table('episodes',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('iteration', sa.Integer(), nullable=False),
    sa.Column('goal_summary', sa.Text(), nullable=False),
    sa.Column('observation_summary', sa.Text(), nullable=False),
    sa.Column('decision_type', sa.String(length=20), nullable=False),
    sa.Column('plan_step_id', sa.String(length=100), nullable=True),
    sa.Column('action_id', sa.Uuid(), nullable=True),
    sa.Column('tool_name', sa.String(length=100), nullable=True),
    sa.Column('result_summary', sa.Text(), nullable=False),
    sa.Column('verification', JsonType, nullable=False),
    sa.Column('error_code', sa.String(length=40), nullable=True),
    sa.Column('reward', sa.Float(), nullable=False),
    sa.Column('importance', sa.Float(), nullable=False),
    sa.Column('tags', JsonType, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('episodes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_episodes_error_code'), ['error_code'], unique=False)
        batch_op.create_index(batch_op.f('ix_episodes_importance'), ['importance'], unique=False)
        batch_op.create_index('ix_episodes_run_iteration', ['run_id', 'iteration'], unique=True)
        batch_op.create_index(batch_op.f('ix_episodes_tool_name'), ['tool_name'], unique=False)

    op.create_table('observations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('source_ref', sa.String(length=200), nullable=True),
    sa.Column('kind', sa.String(length=80), nullable=False),
    sa.Column('content', JsonType, nullable=False),
    sa.Column('content_hash', sa.String(length=80), nullable=False),
    sa.Column('trust', sa.String(length=20), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('tags', JsonType, nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('meta', JsonType, nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('observations', schema=None) as batch_op:
        batch_op.create_index('ix_observations_pending', ['run_id', 'consumed_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_observations_trust'), ['trust'], unique=False)

    op.create_table('plans',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('objective', sa.Text(), nullable=False),
    sa.Column('steps', JsonType, nullable=False),
    sa.Column('assumptions', JsonType, nullable=False),
    sa.Column('completion_condition', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('invalidated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('plans', schema=None) as batch_op:
        batch_op.create_index('ix_plans_active', ['run_id', 'is_active'], unique=False)

    op.create_table('run_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('iteration', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=60), nullable=False),
    sa.Column('from_phase', sa.String(length=20), nullable=True),
    sa.Column('to_phase', sa.String(length=20), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('error_code', sa.String(length=40), nullable=True),
    sa.Column('payload', JsonType, nullable=False),
    sa.Column('trace_id', sa.String(length=64), nullable=True),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('run_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_run_events_kind'), ['kind'], unique=False)
        batch_op.create_index('ix_run_events_run_at', ['run_id', 'at'], unique=False)
        batch_op.create_index(batch_op.f('ix_run_events_trace_id'), ['trace_id'], unique=False)

    op.create_table('action_attempts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('action_id', sa.Uuid(), nullable=False),
    sa.Column('attempt_no', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('result', JsonType, nullable=True),
    sa.Column('error_code', sa.String(length=40), nullable=True),
    sa.Column('error_detail', sa.Text(), nullable=True),
    sa.Column('probe_outcome', JsonType, nullable=True),
    sa.ForeignKeyConstraint(['action_id'], ['actions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('action_attempts', schema=None) as batch_op:
        batch_op.create_index('ix_attempts_in_flight', ['status'], unique=False)
        batch_op.create_index('uq_attempt_no', ['action_id', 'attempt_no'], unique=True)



def downgrade() -> None:
    with op.batch_alter_table('action_attempts', schema=None) as batch_op:
        batch_op.drop_index('uq_attempt_no')
        batch_op.drop_index('ix_attempts_in_flight')

    op.drop_table('action_attempts')
    with op.batch_alter_table('run_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_run_events_trace_id'))
        batch_op.drop_index('ix_run_events_run_at')
        batch_op.drop_index(batch_op.f('ix_run_events_kind'))

    op.drop_table('run_events')
    with op.batch_alter_table('plans', schema=None) as batch_op:
        batch_op.drop_index('ix_plans_active')

    op.drop_table('plans')
    with op.batch_alter_table('observations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_observations_trust'))
        batch_op.drop_index('ix_observations_pending')

    op.drop_table('observations')
    with op.batch_alter_table('episodes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_episodes_tool_name'))
        batch_op.drop_index('ix_episodes_run_iteration')
        batch_op.drop_index(batch_op.f('ix_episodes_importance'))
        batch_op.drop_index(batch_op.f('ix_episodes_error_code'))

    op.drop_table('episodes')
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.drop_index('uq_actions_idempotency')
        batch_op.drop_index(batch_op.f('ix_actions_logical_action_id'))
        batch_op.drop_index('ix_actions_fingerprint')

    op.drop_table('actions')
    with op.batch_alter_table('agent_runs', schema=None) as batch_op:
        batch_op.drop_index('ix_runs_lease')
        batch_op.drop_index(batch_op.f('ix_agent_runs_phase'))
        batch_op.drop_index(batch_op.f('ix_agent_runs_goal_id'))

    op.drop_table('agent_runs')
    with op.batch_alter_table('goals', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_goals_status'))
        batch_op.drop_index('ix_goals_agent_status')

    op.drop_table('goals')
    op.drop_table('skills')
    op.drop_table('agents')
