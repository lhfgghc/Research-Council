import { useEffect, useState } from "react";
import { api } from "../api";
import type { Project, RunBudgetPreview, RunSummary, Settings, Skill } from "../types";
import { Icon } from "./Icons";

export function RunSetup({
  project,
  settings,
  skills,
  onStarted,
}: {
  project: Project;
  settings: Settings | null;
  skills: Skill[];
  onStarted: (run: RunSummary) => void;
}) {
  const [ideaCount, setIdeaCount] = useState(3);
  const [candidateCount, setCandidateCount] = useState(6);
  const [teamRounds, setTeamRounds] = useState(2);
  const [maxCalls, setMaxCalls] = useState(50);
  const [maxTeamTurns, setMaxTeamTurns] = useState(20);
  const [maxDebateTurns, setMaxDebateTurns] = useState(15);
  const [deliverable, setDeliverable] = useState<"idea_brief" | "manuscript">("idea_brief");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<RunBudgetPreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [writingSkills, setWritingSkills] = useState<string[] | null>(null);
  const effectiveWriting = writingSkills ?? settings?.writing_skills ?? [];
  const pi = settings?.model_profiles.find((profile) => profile.id === settings.role_models.pi?.[0]);

  useEffect(() => {
    const controller = new AbortController();
    setPreview(null);
    setPreviewError("");
    const timer = setTimeout(() => {
      api.previewRunBudget({
        idea_count: ideaCount,
        candidate_count: candidateCount,
        team_rounds: teamRounds,
        max_calls: maxCalls,
        max_team_turns: maxTeamTurns,
        max_debate_turns: maxDebateTurns,
        deliverable,
      }, controller.signal).then(setPreview).catch((error: unknown) => {
        if (!controller.signal.aborted) setPreviewError(error instanceof Error ? error.message : "无法计算预算");
      });
    }, 150);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [candidateCount, deliverable, ideaCount, maxCalls, maxTeamTurns, maxDebateTurns, teamRounds, settings]);

  async function start() {
    setBusy(true);
    setError("");
    try {
      const run = await api.createRun(project.id, {
        idea_count: ideaCount,
        candidate_count: candidateCount,
        team_rounds: teamRounds,
        max_calls: maxCalls,
        max_team_turns: maxTeamTurns,
        max_debate_turns: maxDebateTurns,
        deliverable,
        writing_skills: writingSkills,
      });
      onStarted(run);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "启动失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="run-setup">
      <div className="run-setup-intro">
        <span className="eyebrow">准备一次科研会议</span>
        <h2>让多模型辩出值得验证的 Idea</h2>
        <p>
          PI 会先拆出研究问题与议程。研究者独立提出候选，团队讨论后进行取舍，
          每个入选 Idea 会独立辩论，并交付首轮验证方案。
        </p>
        <div className="run-sequence">
          <span>独立候选</span>
          <Icon name="chevron" size={14} />
          <span>团队讨论</span>
          <Icon name="chevron" size={14} />
          <span>分 Idea 深化</span>
          <Icon name="chevron" size={14} />
          <span>验证方案与总览</span>
        </div>
      </div>

      <div className="run-setup-panel">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">运行配置</span>
            <h3>{project.venue} · 方案生成</h3>
          </div>
          <span className="model-chip">
            <Icon name="cpu" size={14} />
            {pi ? `负责人 · ${pi.name || pi.model}` : "负责人未分配模型"}
          </span>
        </div>

        <div className="form-grid two">
          <label className="field compact">
            <span>完整 Idea 数</span>
            <input
              type="number"
              min={1}
              max={6}
              value={ideaCount}
              onChange={(event) => setIdeaCount(Number(event.target.value))}
            />
          </label>
          <label className="field compact">
            <span>独立候选数</span>
            <input
              type="number"
              min={2}
              max={12}
              value={candidateCount}
              onChange={(event) => setCandidateCount(Number(event.target.value))}
            />
          </label>
          <label className="field compact">
            <span>{settings?.discussion_models ? "每个模型最低发言次数" : "每角色最低发言次数"}</span>
            <input
              type="number"
              min={1}
              value={teamRounds}
              onChange={(event) => setTeamRounds(Math.max(1, Number(event.target.value)))}
            />
          </label>
          <label className="field compact">
            <span>最大 LLM 调用</span>
            <input
              type="number"
              min={8}
              max={200}
              value={maxCalls}
              onChange={(event) => setMaxCalls(Number(event.target.value))}
            />
          </label>
        </div>

        <p className="run-setup-hint">
          发言目标用于保证参与。随后由点名与未决争议决定谁接话，剩余调用额度决定还能讨论多久。
        </p>

        <details>
          <summary>讨论上限</summary>
          <div className="form-grid two">
            <label className="field compact">
              <span>团队讨论最多发言</span>
              <input type="number" min={0} max={200} value={maxTeamTurns} onChange={(event) => setMaxTeamTurns(Number(event.target.value))} />
            </label>
            <label className="field compact">
              <span>每个 Idea 辩论最多发言</span>
              <input type="number" min={0} max={100} value={maxDebateTurns} onChange={(event) => setMaxDebateTurns(Number(event.target.value))} />
            </label>
          </div>
          <small>填 0 时由调用预算和讨论进展决定收束。</small>
        </details>

        <label className="field compact">
          <span>交付形式</span>
          <select value={deliverable} onChange={(event) => setDeliverable(event.target.value === "manuscript" ? "manuscript" : "idea_brief")}>
            <option value="idea_brief">Idea 方案与首轮验证</option>
            <option value="manuscript">论文式完整方案</option>
          </select>
        </label>

        <details>
          <summary>本次写作 Skills（{effectiveWriting.length}）</summary>
          {[...new Set([...effectiveWriting, ...skills.map((skill) => skill.id)])].sort().map((id) => (
            <label className="field compact" key={id}>
              <span>{id}</span>
              <input type="checkbox" checked={effectiveWriting.includes(id)} onChange={(event) => {
                setWritingSkills(event.target.checked
                  ? [...effectiveWriting, id]
                  : effectiveWriting.filter((item) => item !== id));
              }} />
            </label>
          ))}
          {writingSkills !== null && <button className="text-button" onClick={() => setWritingSkills(null)}>恢复全局设置</button>}
        </details>

        {preview && (
          <div className={`budget-note ${maxCalls < preview.full || preview.error ? "warning" : ""}`}>
            <span>核心流程 <strong>{preview.core}</strong> 次。团队讨论 {preview.team_models} 个{settings?.discussion_models ? "模型" : "角色"}，合计 {preview.team} 次发言。逐 Idea 辩论合计 {preview.debate} 次。{preview.panels > 0 && `面板额外 ${preview.panels} 次。`}完整覆盖约 <strong>{preview.full}</strong> 次，建议 <strong>{preview.recommended}</strong> 次。</span>
          </div>
        )}
        {preview && preview.debate_models < 2 && (
          <p className="run-setup-hint">逐 Idea 辩论需要至少两个模型。可在模型团队设置中增加讨论成员。</p>
        )}

        {(error || previewError || preview?.error) && <p className="form-error">{error || previewError || preview?.error}</p>}
        <button className="button primary wide" onClick={start} disabled={busy || !preview || Boolean(preview.error)}>
          {busy ? "正在创建讨论…" : "启动多 Agent 讨论"}
          {!busy && <Icon name="play" size={16} />}
        </button>
      </div>
    </section>
  );
}
