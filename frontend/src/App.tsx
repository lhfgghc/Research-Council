import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { DiscussionView } from "./components/DiscussionView";
import { IdeaWorkspace } from "./components/IdeaWorkspace";
import { Icon } from "./components/Icons";
import { NewProjectModal } from "./components/NewProjectModal";
import { ProjectSidebar } from "./components/ProjectSidebar";
import { HomeView } from "./components/HomeView";
import { RunSetup } from "./components/RunSetup";
import { SettingsView } from "./components/SettingsView";
import { Badge, EmptyState } from "./components/UI";
import type { Overview, Project, ProjectDetail, RunDetail, Settings, Skill } from "./types";
import { formatDate, statusLabel, statusTone } from "./utils";

type View = "home" | "workspace" | "settings";

export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [health, setHealth] = useState<{
    api_configured: boolean;
    configured_models: number;
    model: string;
    database_writable?: boolean;
    database_error?: string;
  } | null>(null);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
  const [view, setView] = useState<View>("home");
  const [showNewProject, setShowNewProject] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshTick, setRefreshTick] = useState(0);
  const [overview, setOverview] = useState<Overview | null>(null);

  const refreshProjects = useCallback(async () => {
    const rows = await api.listProjects();
    setProjects(rows);
    setSelectedProjectId((current) => {
      if (current && rows.some((project) => project.id === current)) return current;
      return rows[0]?.id || null;
    });
  }, []);

  const refreshSkills = useCallback(async () => {
    const payload = await api.listSkills();
    setSkills(payload.skills);
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.listProjects(), api.getSettings(), api.listSkills(), api.health()])
      .then(([projectRows, settingsPayload, skillPayload, healthPayload]) => {
        if (cancelled) return;
        setProjects(projectRows);
        setSelectedProjectId((current) => current || projectRows[0]?.id || null);
        setSettings(settingsPayload);
        setSkills(skillPayload.skills);
        setHealth(healthPayload);
      })
      .catch((exc) => setError(exc instanceof Error ? exc.message : "无法连接后端服务"))
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const bumpRefresh = useCallback(() => setRefreshTick((value) => value + 1), []);

  useEffect(() => {
    if (view !== "home") return;
    let cancelled = false;
    api
      .getOverview()
      .then((payload) => {
        if (!cancelled) setOverview(payload);
      })
      .catch(() => {
        if (!cancelled) setOverview(null);
      });
    return () => {
      cancelled = true;
    };
  }, [view, refreshTick]);

  if (loading) {
    return (
      <div className="boot-screen">
        <span className="brand-mark large">RC</span>
        <p>正在打开 Research Council…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="boot-screen">
        <EmptyState
          title="后端服务未连接"
          description={`${error}。请确认 FastAPI 已在 127.0.0.1:8000 启动。`}
          action={<button className="button primary" onClick={() => window.location.reload()}>重新加载</button>}
        />
      </div>
    );
  }

  return (
    <div className="app-shell">
      <ProjectSidebar
        projects={projects}
        selectedId={view === "workspace" ? selectedProjectId : null}
        health={health}
        onSelect={(id) => {
          setSelectedProjectId(id);
          setView("workspace");
        }}
        onNew={() => setShowNewProject(true)}
        onSettings={() => setView("settings")}
        onHome={() => setView("home")}
      />

      <main className="main-shell">
        {view === "settings" ? (
          <SettingsView
            settings={settings}
            skills={skills}
            onUpdated={(payload) => {
              setSettings(payload);
              void refreshSkills();
              void api.health().then(setHealth);
            }}
            onBack={() => {
              setView("home");
              bumpRefresh();
            }}
          />
        ) : view === "workspace" && selectedProjectId ? (
          <ProjectWorkspace
            key={selectedProjectId}
            projectId={selectedProjectId}
            settings={settings}
            skills={skills}
            refreshTick={refreshTick}
            onRefresh={bumpRefresh}
            onProjectsChanged={refreshProjects}
          />
        ) : (
          <HomeView
            overview={overview}
            health={health}
            onOpenProject={(id) => {
              setSelectedProjectId(id);
              setView("workspace");
            }}
            onCreate={() => setShowNewProject(true)}
            onSettings={() => setView("settings")}
          />
        )}

        {projects.length > 0 && view === "workspace" && selectedProjectId && (
          <button className="floating-new" onClick={() => setShowNewProject(true)} title="新建研究项目">
            <Icon name="plus" size={20} />
          </button>
        )}
      </main>

      {showNewProject && (
        <NewProjectModal
          onClose={() => setShowNewProject(false)}
          onCreated={(project) => {
            setShowNewProject(false);
            setProjects((current) => [project, ...current]);
            setSelectedProjectId(project.id);
            setView("workspace");
          }}
        />
      )}
    </div>
  );
}

function ProjectWorkspace({
  projectId,
  settings,
  skills,
  refreshTick,
  onRefresh,
  onProjectsChanged,
}: {
  projectId: string;
  settings: Settings | null;
  skills: Skill[];
  refreshTick: number;
  onRefresh: () => void;
  onProjectsChanged: () => void;
}) {
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [tab, setTab] = useState<"discussion" | "ideas">("discussion");
  const [showSetup, setShowSetup] = useState(false);
  const [loading, setLoading] = useState(true);
  const [runLoading, setRunLoading] = useState(false);
  const [error, setError] = useState("");
  const [archiveBusy, setArchiveBusy] = useState(false);
  const loadedProjectRef = useRef<string | null>(null);
  const loadedRunRef = useRef<string | null>(null);

  function openRevisionRun(runId: string) {
    setSelectedRunId(runId);
    setTab("discussion");
    onRefresh();
  }

  async function toggleArchive() {
    if (!project) return;
    setArchiveBusy(true);
    setError("");
    try {
      if (project.archived) {
        await api.unarchiveProject(project.id);
      } else {
        await api.archiveProject(project.id);
      }
      onProjectsChanged();
      onRefresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "归档操作失败");
    } finally {
      setArchiveBusy(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    if (loadedProjectRef.current !== projectId) {
      setLoading(true);
      loadedProjectRef.current = projectId;
    }
    api
      .getProject(projectId)
      .then((payload) => {
        if (cancelled) return;
        setProject(payload);
        setSelectedRunId((current) => {
          if (current && payload.runs.some((item) => item.id === current)) return current;
          return payload.runs[0]?.id || null;
        });
        if (payload.runs.length === 0) setShowSetup(true);
      })
      .catch((exc) => {
        if (!cancelled) setError(exc instanceof Error ? exc.message : "加载项目失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, refreshTick]);

  useEffect(() => {
    if (!selectedRunId) {
      setRun(null);
      loadedRunRef.current = null;
      return;
    }
    let cancelled = false;
    if (loadedRunRef.current !== selectedRunId) {
      setRunLoading(true);
      loadedRunRef.current = selectedRunId;
    }
    api
      .getRun(selectedRunId)
      .then((payload) => {
        if (!cancelled) setRun(payload);
      })
      .catch((exc) => {
        if (!cancelled) setError(exc instanceof Error ? exc.message : "加载运行失败");
      })
      .finally(() => {
        if (!cancelled) setRunLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedRunId, refreshTick]);

  useEffect(() => {
    if (run && ["COMPLETED", "COMPLETED_WITH_WARNINGS"].includes(run.status)) {
      void onProjectsChanged();
    }
  }, [run?.status, onProjectsChanged]);

  const latestRun = project?.runs[0] || null;
  const activeRun = useMemo(
    () => project?.runs.find((item) => item.id === selectedRunId) || latestRun,
    [project?.runs, selectedRunId, latestRun],
  );
  const hasActiveRun = project?.runs.some((item) =>
    ["RUNNING", "PAUSED"].includes(item.status),
  );

  if (loading) {
    return <div className="page-loading"><span className="pulse-mark" /> 正在加载项目…</div>;
  }
  if (!project) {
    return <div className="page-loading">{error || "项目不存在"}</div>;
  }

  const shouldShowSetup = showSetup || project.runs.length === 0;

  return (
    <div className="project-page">
      <header className="project-header">
        <div className="project-heading">
          <div className="breadcrumb">
            <span>{project.domain || "Research"}</span>
            <Icon name="chevron" size={13} />
            <span>{project.venue}</span>
          </div>
          <div className="project-title-row">
            <h1>{project.name}</h1>
            {project.archived && <Badge tone="neutral">已归档</Badge>}
            {activeRun && (
              <Badge tone={statusTone(activeRun.status)}>{statusLabel(activeRun.status)}</Badge>
            )}
          </div>
          {project.topic.trim() && (
            <details className="project-brief">
              <summary><Icon name="chevron" size={13} />项目说明</summary>
              <p>{project.topic}</p>
            </details>
          )}
          <div className="project-submeta">
            <span><Icon name="file" size={13} /> {project.venue}</span>
            <span><Icon name="book" size={13} /> {project.idea_count_actual} ideas</span>
            <span><Icon name="clock" size={13} /> {formatDate(project.updated_at)}</span>
          </div>
        </div>
        <div className="project-header-actions">
          {project.runs.length > 1 && (
            <select
              className="run-select"
              value={selectedRunId || ""}
              onChange={(event) => {
                setSelectedRunId(event.target.value);
                setTab("discussion");
                setShowSetup(false);
              }}
            >
              {project.runs.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.mode === "REVISE" ? "修订" : "生成"} · {formatDate(item.created_at)} · {statusLabel(item.status)}
                </option>
              ))}
            </select>
          )}
          <button
            className="button ghost"
            onClick={toggleArchive}
            disabled={archiveBusy}
            title={project.archived ? "恢复为活动项目" : "归档后项目移入侧栏“已归档”分组，可随时恢复"}
          >
            <Icon name="folder" size={16} />
            {project.archived ? "取消归档" : "归档项目"}
          </button>
          <button
            className="button ghost"
            onClick={() => {
              setShowSetup(true);
              setTab("discussion");
            }}
            disabled={Boolean(hasActiveRun && !showSetup) || Boolean(project.archived)}
            title={project.archived ? "已归档项目，取消归档后可新建运行" : hasActiveRun ? "请先完成或终止当前运行" : ""}
          >
            <Icon name="plus" size={16} /> 新建运行
          </button>
        </div>
      </header>

      {!shouldShowSetup && (
        <nav className="project-tabs">
          <button className={tab === "discussion" ? "active" : ""} onClick={() => setTab("discussion")}>
            <Icon name="message" size={16} /> 讨论现场
          </button>
          <button className={tab === "ideas" ? "active" : ""} onClick={() => setTab("ideas")}>
            <Icon name="book" size={16} /> Idea 文档
            <span className="tab-count">{project.ideas.length}</span>
          </button>
          <div className="project-tabs-meta">
            {activeRun && (
              <span>
                {activeRun.model} · 预算 {activeRun.budget_used}/{activeRun.budget_max}
              </span>
            )}
          </div>
        </nav>
      )}

      {error && <div className="inline-alert">{error}</div>}

      {shouldShowSetup ? (
        <RunSetup
          project={project}
          settings={settings}
          skills={skills}
          onStarted={(created) => {
            setShowSetup(false);
            setSelectedRunId(created.id);
            setTab("discussion");
            onRefresh();
          }}
        />
      ) : tab === "ideas" ? (
        <IdeaWorkspace
          project={project}
          refreshKey={refreshTick}
          onStartRun={() => setShowSetup(true)}
          onFeedbackCreated={openRevisionRun}
        />
      ) : run ? (
        <DiscussionView
          key={run.id}
          run={run}
          refreshKey={refreshTick}
          onRefresh={onRefresh}
          onFeedbackCreated={openRevisionRun}
        />
      ) : (
        <div className="page-loading">
          <span className="pulse-mark" /> {runLoading ? "正在加载讨论…" : "选择一次运行"}
        </div>
      )}
    </div>
  );
}
