import type { CodeTask, ConnectionStatus, LogEntry, TaskEvent } from '../types';
import { describeEvent } from '../utils/eventSummary';
export interface AppState { connectionStatus: ConnectionStatus; task: CodeTask | null; events: TaskEvent[]; logs: LogEntry[]; streamError: string | null; }
export const initialAppState: AppState = { connectionStatus: 'idle', task: null, events: [], streamError: null, logs: [{ id: 0, type: 'system', timestamp: new Date().toLocaleTimeString(), phase: '系统就绪', message: 'VeriForge 已就绪', status: 'ok' }] };
export type AppAction = { type: 'TASK_CREATED'; payload: CodeTask } | { type: 'TASK_SELECTED'; payload: CodeTask } | { type: 'TASK_UPDATED'; payload: CodeTask } | { type: 'EVENT'; payload: TaskEvent } | { type: 'REPLAY'; payload: TaskEvent[] } | { type: 'CONNECTION'; payload: ConnectionStatus } | { type: 'STREAM_ABORTED'; payload: string } | { type: 'ERROR'; payload: string };
// 日志 id 必须全局唯一：不能用数组下标（过滤后 id 会撞 React key）
let nextLogId = 1;
function makeLog(message: string, type: LogEntry['type'], timestamp: string, phase: string): LogEntry {
  return { id: nextLogId++, type, timestamp, phase, message, status: type === 'failed' ? 'err' : 'ok' };
}
function log(state: AppState, message: string, type: LogEntry['type']='system'): LogEntry[] { return [...state.logs, makeLog(message, type, new Date().toLocaleTimeString(), '任务事件')]; }
function maxEventSeq(events: TaskEvent[]): number {
  return events.reduce((max, event) => (event.event_seq > max ? event.event_seq : max), 0);
}

/**
 * 事件日志是唯一事实源：SSE（至少一次投递）与 /events 自动同步都汇到这里。
 * - 按 event_seq 去重、按序号保序
 * - 同序号的记录若本地已带 message（来自回放）则保留本地引用，
 *   这样「内容没变」时返回值与原 state 逐项全等 → 轮询同步不会引发重渲染
 */
function mergeEvents(current: TaskEvent[], incoming: TaskEvent[]): TaskEvent[] {
  if (incoming.length === 0) return current;
  const bySeq = new Map<number, TaskEvent>();
  for (const event of current) bySeq.set(event.event_seq, event);
  for (const event of incoming) {
    const existing = bySeq.get(event.event_seq);
    if (!existing || (!existing.message && event.message)) bySeq.set(event.event_seq, event);
  }
  return [...bySeq.values()].sort((a, b) => a.event_seq - b.event_seq);
}

export function appReducer(state: AppState, action: AppAction): AppState {
  switch(action.type) {
    case 'TASK_CREATED':
      return {...state, task: action.payload, events: [], streamError: null, connectionStatus: 'connecting', logs: log(state, `已创建代码任务 #${action.payload.id}`)};
    case 'TASK_SELECTED':
      // 从任务列表切回来：先清空事件，SSE 重连会全量重放，patch 等派生状态随之重建
      return {...state, task: action.payload, events: [], streamError: null, connectionStatus: 'connecting', logs: log(state, `已切换到任务 #${action.payload.id}`)};
    case 'TASK_UPDATED':
      return {...state, task: action.payload};
    case 'EVENT': {
      // SSE 是「至少一次投递」：重连会重放旧事件，按 event_seq 去重（D6）
      const incoming = action.payload;
      if (incoming.event_seq !== undefined && incoming.event_seq <= maxEventSeq(state.events)) return state;
      const isFailure = incoming.event_type === 'TASK_FAILED';
      return {...state, events: mergeEvents(state.events, [incoming]), task: state.task ? {...state.task, status: String(incoming.payload.status ?? state.task.status)} : state.task, connectionStatus: incoming.event_type === 'TASK_STARTED' ? 'connected' : state.connectionStatus, logs: log(state, describeEvent(incoming), isFailure ? 'failed' : 'system')};
    }
    case 'REPLAY': {
      // 自动同步（取代「回放历史事件」按钮）：纯粹对齐事实源，不写日志、不打扰
      const merged = mergeEvents(state.events, action.payload);
      if (merged.length === state.events.length && merged.every((event, index) => event === state.events[index])) return state;
      return {...state, events: merged};
    }
    case 'CONNECTION':
      return {...state, connectionStatus: action.payload, streamError: action.payload === 'connected' ? null : state.streamError};
    case 'STREAM_ABORTED':
      return {...state, connectionStatus: 'error', streamError: action.payload, logs: log(state, action.payload, 'failed')};
    case 'ERROR':
      return {...state, connectionStatus:'error', logs:log(state, action.payload,'failed')};
    default: return state;
  }
}
