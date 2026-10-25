import { describe, expect, it } from 'vitest';
import { appReducer, initialAppState } from './appReducer';
import type { TaskEvent } from '../types';

function makeEvent(seq: number, overrides: Partial<TaskEvent> = {}): TaskEvent {
  return {
    event_seq: seq,
    event_type: 'TASK_STARTED',
    message: `事件 ${seq}`,
    payload: { type: 'TASK_STARTED' },
    created_at: 'now',
    ...overrides,
  };
}

describe('VeriForge task reducer', () => {
  it('stores a created code task and starts connection state', () => {
    const task = { id: 1, repository_id: 2, issue_title: '修复校验', issue_body: '补充边界检查', status: 'pending', attempt_number: 0, created_at: 'now' };
    const next = appReducer(initialAppState, { type: 'TASK_CREATED', payload: task });
    expect(next.task).toEqual(task);
    expect(next.connectionStatus).toBe('connecting');
  });
  it('merges synced events into the live ones without duplicating or re-logging them', () => {
    // SSE 路径的帧不带 message，/events 同步拿到的记录更全
    let state = appReducer(initialAppState, { type: 'EVENT', payload: makeEvent(1, { message: null }) });
    const logsBefore = state.logs.length;

    const durable = { ...makeEvent(1), message: '服务端持久化描述' };
    state = appReducer(state, { type: 'REPLAY', payload: [durable, makeEvent(2)] });

    expect(state.events.map((event) => event.event_seq)).toEqual([1, 2]);
    expect(state.events[0].message).toBe('服务端持久化描述');
    // 同步是静默的：不往日志里塞条目
    expect(state.logs).toHaveLength(logsBefore);
  });
  it('returns the very same state when a resync brings nothing new (keeps polling cheap)', () => {
    const durable = { ...makeEvent(1), message: '服务端持久化描述' };
    const second = { ...makeEvent(2), message: '第二条' };
    const once = appReducer(initialAppState, { type: 'REPLAY', payload: [durable, second] });
    const twice = appReducer(once, { type: 'REPLAY', payload: [{ ...durable }, { ...second }] });
    expect(twice).toBe(once);
  });
  it('switching to another task resets the event stream and reconnects', () => {
    const withEvent = appReducer(initialAppState, { type: 'EVENT', payload: makeEvent(1) });
    const other = { id: 7, repository_id: 1, issue_title: 't', issue_body: 'b', status: 'awaiting_approval', attempt_number: 1, created_at: 'now' };
    const switched = appReducer(withEvent, { type: 'TASK_SELECTED', payload: other });
    expect(switched.task).toEqual(other);
    expect(switched.events).toEqual([]);
    expect(switched.connectionStatus).toBe('connecting');
    expect(switched.streamError).toBeNull();
  });
  it('drops events whose event_seq was already seen (SSE at-least-once delivery)', () => {
    let state = appReducer(initialAppState, { type: 'EVENT', payload: makeEvent(5) });
    state = appReducer(state, { type: 'EVENT', payload: makeEvent(6) });
    const duplicated = appReducer(state, { type: 'EVENT', payload: makeEvent(6, { message: '重复推送' }) });
    expect(duplicated.events).toHaveLength(2);
    expect(duplicated.events.map((e) => e.event_seq)).toEqual([5, 6]);
    expect(duplicated.logs).toHaveLength(state.logs.length);

    const fresh = appReducer(duplicated, { type: 'EVENT', payload: makeEvent(7) });
    expect(fresh.events).toHaveLength(3);
  });
  it('surfaces an aborted event stream and clears it when a new task starts', () => {
    const aborted = appReducer(initialAppState, { type: 'STREAM_ABORTED', payload: '事件流已中断' });
    expect(aborted.connectionStatus).toBe('error');
    expect(aborted.streamError).toBe('事件流已中断');
    expect(aborted.logs.at(-1)?.status).toBe('err');

    const task = { id: 3, repository_id: 1, issue_title: 't', issue_body: 'b', status: 'pending', attempt_number: 0, created_at: 'now' };
    const restarted = appReducer(aborted, { type: 'TASK_CREATED', payload: task });
    expect(restarted.streamError).toBeNull();
  });
  it('replaces the task snapshot without touching the event stream', () => {
    const created = appReducer(initialAppState, { type: 'TASK_CREATED', payload: { id: 9, repository_id: 1, issue_title: 't', issue_body: 'b', status: 'pending', attempt_number: 0, created_at: 'now' } });
    const withEvent = appReducer(created, { type: 'EVENT', payload: makeEvent(1) });
    const updated = appReducer(withEvent, { type: 'TASK_UPDATED', payload: { ...withEvent.task!, status: 'awaiting_approval', plan_decision: 'approved' } });
    expect(updated.task?.plan_decision).toBe('approved');
    expect(updated.events).toHaveLength(1);
  });
});
