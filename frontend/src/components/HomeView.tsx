import type { Overview } from "../types";
import { formatDate, statusLabel, statusTone, truncate } from "../utils";
import { Icon } from "./Icons";
import { Badge } from "./UI";

export function HomeView({
  overview,
  health,
  onOpenProject,
  onCreate,
  onSettings,
}: {
  overview: Overview | null;
  health: {
    api_configured: boolean;
    configured_models: number;
    model: string;
    database_writable?: boolean;
    database_error?: string;
  } | null;
  onOpenProject: (id: string) => void;
  onCreate: () => void;
  onSettings: () => void;
}) {
  const stats = [
    {
      label: "研究项目",
      value: overview ? String(overview.projects.active) : "—",
      note: overview?.projects.archived ? `${overview.projects.archived} 个已归档` : "活动项目",
    },
    {
      label: "Idea 方案",
      value: overview ? String(overview.ideas) : "—",
      note: "已落盘的版本化方案",
    },
    {
      label: "讨论运行",
      value: overview ? String(overview.runs.total) : "—",
      note: overview?.runs.running ? `${overview.runs.running} 个进行中` : "生成与修订",
    },
    {
      label: "LLM 调用",
      value: overview ? String(overview.calls.total) : "—",
      note: overview ? `${formatTokens(overview.calls.prompt_tokens)} 输入` : "统一网关计数",
    },
    {
      label: "估算成本",
      value: overview ? `$${overview.calls.cost_usd.toFixed(3)}` : "—",
      note: "按设置中的单价估算",
    },
  ];

  const recent = overview?.recent_projects || [];
  const team = overview?.model_team;

  return (
    <div className="home-page">
      <section className="home-hero">
        <div className="home-hero-copy">
          <span className="eyebrow">CCF-A RESEARCH IDEATION · MULTI-AGENT</span>
          <h1>
            把研究讨论，
            <br />
            沉淀成论文式方案。
          </h1>
          <p>
            负责人设定议程，专业研究者独立提出候选，团队在有限轮次内取舍。
            每个 Idea 都经过文献调研、方法、实验、成稿和一次一致性检查，最终以
            Markdown 文段落盘。
          </p>
          <div className="home-actions">
            <button className="button primary large" onClick={onCreate}>
              <Icon name="plus" size={18} /> 建立研究项目
            </button>
            <button className="button ghost large" onClick={onSettings}>
              <Icon name="settings" size={17} /> 配置模型与 Skills
            </button>
          </div>
        </div>

        <aside className="home-status">
          <div className="home-status-head">
            <span className="eyebrow">系统状态</span>
            <span className={`status-dot ${health?.api_configured ? "positive" : "danger"}`} />
          </div>
          <div className="status-row">
            <span>模型配置</span>
            <strong>{health?.configured_models ? `${health.configured_models} 个已配置` : "未配置"}</strong>
          </div>
          <div className="status-row">
            <span>数据库</span>
            <strong>{health?.database_writable === false ? "只读" : "可写"}</strong>
          </div>
          <div className="home-team">
            <span className="eyebrow">模型团队</span>
            <div className="home-team-list">
              {(team?.profiles || []).slice(0, 4).map((profile) => (
                <span className="source-chip" key={profile.id} title={profile.model}>
                  {profile.name}
                </span>
              ))}
              {(!team?.profiles || team.profiles.length === 0) && (
                <span className="muted">尚未配置模型档案</span>
              )}
            </div>
          </div>
        </aside>
      </section>

      <section className="home-stats" aria-label="使用概览">
        {stats.map((item) => (
          <div className="home-stat" key={item.label}>
            <span className="home-stat-label">{item.label}</span>
            <strong>{item.value}</strong>
            <small>{item.note}</small>
          </div>
        ))}
      </section>

      <section className="home-recent">
        <div className="home-section-head">
          <h2>最近项目</h2>
          <span className="muted">按最近更新排序，点击进入讨论现场</span>
        </div>
        {recent.length === 0 ? (
          <div className="home-empty">
            {overview && overview.projects.total > 0 ? (
              <>
                <p>
                  所有项目都已归档。可以在左侧“已归档”分组中打开并取消归档，
                  或建立一个新的研究项目。
                </p>
                <button className="button primary" onClick={onCreate}>
                  <Icon name="plus" size={16} /> 建立新项目
                </button>
              </>
            ) : (
              <>
                <p>还没有研究项目。建立第一个项目后，这里会显示最近的活动。</p>
                <button className="button primary" onClick={onCreate}>
                  <Icon name="plus" size={16} /> 建立第一个项目
                </button>
              </>
            )}
          </div>
        ) : (
          <div className="home-project-grid">
            {recent.map((project) => (
              <button
                className="home-project-card"
                key={project.id}
                onClick={() => onOpenProject(project.id)}
              >
                <div className="home-project-top">
                  <span className="home-project-name">{project.name}</span>
                  <span className={`status-dot ${statusTone(project.latest_run?.status)}`} />
                </div>
                <p>{truncate(project.topic, 90)}</p>
                <div className="home-project-meta">
                  <span>{project.venue}</span>
                  <span>{project.idea_count_actual} ideas</span>
                  <span>{formatDate(project.updated_at)}</span>
                </div>
                <div className="home-project-foot">
                  <Badge tone={statusTone(project.latest_run?.status)}>
                    {statusLabel(project.latest_run?.status)}
                  </Badge>
                  <span className="home-project-open">
                    进入 <Icon name="chevron" size={12} />
                  </span>
                </div>
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M tok`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k tok`;
  return `${value} tok`;
}
