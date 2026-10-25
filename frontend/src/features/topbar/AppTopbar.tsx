import { useTheme } from '../theme/useTheme';

interface AppTopbarProps {
  sidebarOpen: boolean;
  canCreateTask: boolean;
  onToggleSidebar: () => void;
  onOpenRepoForm: () => void;
  onOpenTaskForm: () => void;
}

export function AppTopbar({ sidebarOpen, canCreateTask, onToggleSidebar, onOpenRepoForm, onOpenTaskForm }: AppTopbarProps) {
  const { mode, toggle, label, icon } = useTheme();
  return (
    <header className="app-topbar">
      <button
        type="button"
        className="sidebar-toggle"
        aria-expanded={sidebarOpen}
        aria-controls="workspace-sidebar"
        onClick={onToggleSidebar}
      >
        工作区
      </button>

      <div className="app-brand">
        <h1>VeriForge</h1>
        <p>可追踪、可审查的代码维护任务平台</p>
      </div>

      <div className="topbar-actions">
        <button
          type="button"
          className="theme-toggle"
          onClick={toggle}
          aria-label={`切换到${mode === 'light' ? '深色' : '浅色'}主题，当前：${label}`}
          title={`主题：${label}（点击切换）`}
        >
          {icon}
        </button>
        <button type="button" className="btn-round" onClick={onOpenRepoForm}>配置仓库</button>
        <button type="button" className="btn-round-primary" disabled={!canCreateTask} onClick={onOpenTaskForm}>新建任务</button>
      </div>
    </header>
  );
}
