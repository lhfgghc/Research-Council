import { useState } from "react";
import type { Project } from "../types";
import { formatDate, statusLabel, statusTone, truncate } from "../utils";
import { Icon } from "./Icons";

export function ProjectSidebar({
  projects,
  selectedId,
  health,
  onSelect,
  onNew,
  onSettings,
  onHome,
}: {
  projects: Project[];
  selectedId: string | null;
  health: {
    api_configured: boolean;
    configured_models: number;
    model: string;
    database_writable?: boolean;
    database_error?: string;
  } | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onSettings: () => void;
  onHome: () => void;
}) {
  const [query, setQuery] = useState("");
  const keyword = query.trim().toLowerCase();
  const matches = (project: Project) =>
    !keyword ||
    [project.name, project.venue, project.domain, project.topic]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(keyword));

  const active = projects.filter((project) => !project.archived && matches(project));
  const archived = projects.filter((project) => project.archived && matches(project));

  const renderItem = (project: Project) => (
    <button
      key={project.id}
      className={`project-nav-item ${selectedId === project.id ? "active" : ""} ${
        project.archived ? "archived" : ""
      }`}
      onClick={() => onSelect(project.id)}
      title={project.archived ? "已归档项目" : undefined}
    >
      <span className="project-nav-icon">
        <Icon name="folder" size={17} />
      </span>
      <span className="project-nav-copy">
        <strong>{project.name}</strong>
        <small>
          {project.venue} · {formatDate(project.updated_at)}
        </small>
      </span>
      <span className={`status-dot ${statusTone(project.latest_run?.status)}`} />
    </button>
  );

  return (
    <aside className="sidebar">
      <button className="brand" onClick={onHome}>
        <span className="brand-mark">RC</span>
        <span>
          <strong>Research Council</strong>
          <small>多智能体科研方案工作台</small>
        </span>
      </button>

      <div className="sidebar-section-head">
        <span>研究项目</span>
        <button className="icon-button subtle" onClick={onNew} aria-label="新建项目">
          <Icon name="plus" size={17} />
        </button>
      </div>

      <div className="sidebar-search">
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索项目（名称 / 会议 / 领域）"
          aria-label="搜索项目"
        />
      </div>

      <nav className="project-nav" aria-label="项目列表">
        {active.length === 0 && archived.length === 0 && (
          <div className="sidebar-empty">
            {keyword ? (
              <p>没有匹配「{query.trim()}」的项目。</p>
            ) : (
              <>
                <p>还没有研究项目。</p>
                <button className="text-button" onClick={onNew}>
                  创建第一个项目
                </button>
              </>
            )}
          </div>
        )}
        {!keyword && active.length === 0 && archived.length > 0 && (
          <p className="muted" style={{ padding: "4px 2px" }}>
            全部项目均已归档。
          </p>
        )}
        {active.map(renderItem)}
        {archived.length > 0 && (
          <details className="archived-group">
            <summary>已归档 · {archived.length}</summary>
            {archived.map(renderItem)}
          </details>
        )}
      </nav>

      <div className="sidebar-footer">
        <div className="connection-card">
          <span className={`status-dot ${health?.api_configured ? "positive" : "danger"}`} />
          <div>
            <strong>{health?.configured_models ? `${health.configured_models} 个模型已配置` : "模型未配置"}</strong>
          </div>
        </div>
        <button className="sidebar-settings" onClick={onSettings}>
          <Icon name="settings" size={17} />
          <span>模型与 Skills 设置</span>
        </button>
        <div className="sidebar-footnote">
          {health?.database_writable === false
            ? `数据库只读：${health.database_error || "无法写入"}`
            : `${active.length} 个项目${archived.length ? ` · ${archived.length} 个已归档` : ""}`}
        </div>
      </div>
    </aside>
  );
}

export function ProjectRailStatus({ project }: { project: Project }) {
  return (
    <div className="rail-status">
      <div>
        <span className="eyebrow">当前状态</span>
        <strong>{statusLabel(project.latest_run?.status)}</strong>
      </div>
      <div>
        <span className="eyebrow">Idea</span>
        <strong>{project.idea_count_actual}</strong>
      </div>
    </div>
  );
}
