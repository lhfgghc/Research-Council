import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { CallSummary, Idea, Message, RunDetail } from "../types";
import { ValidationCard } from "./ValidationCard";
import { DispatchGraph } from "./DispatchGraph";
import { formatDate, formatTokens, phaseLabel, roleInitial, severityLabel, statusLabel, statusTone } from "../utils";
import { DocumentPane, MarkdownView } from "./DocumentPane";
import { FeedbackModal } from "./FeedbackModal";
import { Icon } from "./Icons";
import { Badge, Modal } from "./UI";

const generationPhases = ["P0", "P1", "P1L", "P2", "P3", "P4"].map((phase) => ({
  keys: [phase],
  label: phaseLabel(phase),
}));

const revisionPhases = [
  { keys: ["R0"], label: "反馈分析" },
  { keys: ["R1"], label: "方法修订" },
  { keys: ["R2"], label: "实验修订" },
  { keys: ["R3", "R3A", "R3B"], label: "修订成稿" },
  { keys: ["R4", "R5", "R5A", "R5B"], label: "一致性检查与修复" },
  { keys: ["R6"], label: "保存版本" },
];

const eventNames = [
  "run.started",
  "run.status",
  "run.completed",
  "run.failed",
  "run.budget_exhausted",
  "phase.started",
  "message.created",
  "decision.created",
  "dispatch.changed",
  "artifact.updated",
  "budget.updated",
  "budget.increased",
  "input.queued",
];

const inputLabels: Record<string, string> = {
  GUIDANCE: "方向建议",
  CONSTRAINT: "约束",
  EVIDENCE: "证据或观察",
  DECISION: "明确决策",
};
const maxExtendedCalls = 300;

// 流式内容若是 JSON 契约（P2 讨论/辩论等结构化输出），增量提取 speech 字段作为正文预览；
// 返回 null 表示不是 JSON 输出（成稿类 Markdown 调用照原文展示）。
function extractLiveSpeech(raw: string): { text: string; complete: boolean } | null {
  const trimmed = raw.trimStart();
  if (!trimmed.startsWith("{")) return null;
  const closed = trimmed.match(/"speech"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  if (closed) return { text: closed[1], complete: true };
  const open = trimmed.match(/"speech"\s*:\s*"((?:[^"\\]|\\.)*)$/);
  if (open) return { text: open[1], complete: false };
  return { text: "", complete: false };
}

function unescapeJsonText(text: string): string {
  return text
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "  ")
    .replace(/\\"/g, '"')
    .replace(/\\\\/g, "\\");
}

// 终端式思考流：文本只增不减、内部滚动自动跟底；用户向上滚动查看时暂停跟随，
// 滚回底部附近自动恢复。避免截尾窗口造成的文本跳动。
function LiveThinkingPre({ text }: { text: string }) {
  const ref = useRef<HTMLPreElement | null>(null);
  const stick = useRef(true);
  useEffect(() => {
    const el = ref.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [text]);
  return (
    <pre
      ref={ref}
      className="thinking-text"
      onScroll={(event) => {
        const el = event.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
      }}
    >
      {text}
    </pre>
  );
}

export function DiscussionView({
  run,
  refreshKey,
  onRefresh,
  onFeedbackCreated,
}: {
  run: RunDetail;
  refreshKey: number;
  onRefresh: () => void;
  onFeedbackCreated: (runId: string) => void;
}) {
  const [selectedIdeaId, setSelectedIdeaId] = useState<string | null>(run.ideas[0]?.id || null);
  const [feedbackIdea, setFeedbackIdea] = useState<Idea | null>(null);
  const [input, setInput] = useState("");
  const [inputKind, setInputKind] = useState("GUIDANCE");
  const [sending, setSending] = useState(false);
  const [showCalls, setShowCalls] = useState(false);
  const [showAgenda, setShowAgenda] = useState(false);
  const [showBudget, setShowBudget] = useState(false);
  const [nextBudget, setNextBudget] = useState(0);
  const [savingBudget, setSavingBudget] = useState(false);
  const [budgetError, setBudgetError] = useState("");
  const [actionError, setActionError] = useState("");
  const [focusMode, setFocusMode] = useState(false);
  const [hideThinking, setHideThinking] = useState(false);
  const [showJumpLatest, setShowJumpLatest] = useState(false);
  const [liveCalls, setLiveCalls] = useState<
    Record<string, { purpose: string; roleLabel: string; reasoning: string; content: string }>
  >({});
  const messageEnd = useRef<HTMLDivElement | null>(null);
  const messageStream = useRef<HTMLDivElement | null>(null);
  const refreshTimer = useRef<number | null>(null);
  const lastEventId = useRef(0);
  const stickToBottom = useRef(true);

  const handleStreamScroll = useCallback(() => {
    const el = messageStream.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    setShowJumpLatest(distance > 180);
    // 用户主动向上翻阅后停止粘底；回到底部附近（含点击“回到最新”）自动恢复
    stickToBottom.current = distance < 140;
  }, []);

  function jumpToLatest() {
    const el = messageStream.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    setShowJumpLatest(false);
  }

  function jumpToMessage(id: number) {
    const message = messageStream.current?.querySelector<HTMLElement>(`[data-message-id="${id}"]`);
    if (!message) return;
    stickToBottom.current = false;
    message.focus({ preventScroll: true });
    message.scrollIntoView({ block: "center", behavior: "auto" });
  }

  const phases = run.mode === "REVISE" ? revisionPhases : generationPhases;
  const activePhaseIndex = run.phase === "DONE" ? phases.length : Math.max(
    0,
    phases.findIndex((item) => item.keys.includes(run.phase)),
  );

  useEffect(() => {
    if (!selectedIdeaId && run.ideas[0]) setSelectedIdeaId(run.ideas[0].id);
    if (selectedIdeaId && !run.ideas.some((item) => item.id === selectedIdeaId) && run.ideas[0]) {
      setSelectedIdeaId(run.ideas[0].id);
    }
  }, [run.ideas, selectedIdeaId]);

  const scheduleRefresh = useCallback(() => {
    if (refreshTimer.current) return;
    refreshTimer.current = window.setTimeout(() => {
      refreshTimer.current = null;
      onRefresh();
    }, 1000);
  }, [onRefresh]);

  useEffect(() => {
    const element = messageStream.current;
    if (!element) return;
    const nearBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight < 180;
    if (nearBottom) {
      messageEnd.current?.scrollIntoView({ behavior: "auto", block: "end" });
    }
    handleStreamScroll();
  }, [run.messages.length, handleStreamScroll]);

  // 进入运行或切换运行：无条件定位到最新消息
  useEffect(() => {
    stickToBottom.current = true;
    messageEnd.current?.scrollIntoView({ behavior: "auto", block: "end" });
    handleStreamScroll();
  }, [run.id, handleStreamScroll]);

  // 内容增长（新消息落库 / 流式增量）时保持粘底
  useEffect(() => {
    if (stickToBottom.current) {
      messageEnd.current?.scrollIntoView({ behavior: "auto", block: "end" });
    }
    handleStreamScroll();
  }, [run.messages.length, liveCalls, handleStreamScroll]);

  useEffect(() => {
    const element = messageStream.current;
    if (!element) return;
    element.addEventListener("scroll", handleStreamScroll, { passive: true });
    return () => element.removeEventListener("scroll", handleStreamScroll);
  }, [handleStreamScroll]);

  useEffect(() => {
    const source = new EventSource(`/api/runs/${run.id}/events?after=${lastEventId.current}`);
    const handler = (event: MessageEvent) => {
      const id = Number(event.lastEventId);
      if (Number.isFinite(id) && id > 0) {
        lastEventId.current = Math.max(lastEventId.current, id);
      }
      scheduleRefresh();
    };
    eventNames.forEach((name) => source.addEventListener(name, handler as EventListener));
    source.onopen = scheduleRefresh;
    source.onerror = () => {
      // EventSource reconnects automatically with Last-Event-ID.
    };
    return () => {
      source.close();
      if (refreshTimer.current) {
        window.clearTimeout(refreshTimer.current);
        refreshTimer.current = null;
      }
    };
  }, [run.id, scheduleRefresh]);

  // 订阅进行中模型调用的流式增量：思考与正文实时增长，调用结束后卡片移除，
  // 最终消息仍通过 message.created 事件按普通流程落库展示。
  useEffect(() => {
    const source = new EventSource(`/api/runs/${run.id}/live`);
    source.addEventListener("live.delta", (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent).data) as {
          purpose: string;
          role_label: string;
          reasoning?: string;
          content?: string;
        };
        setLiveCalls((current) => {
          const existing = current[payload.purpose] || {
            purpose: payload.purpose,
            roleLabel: payload.role_label,
            reasoning: "",
            content: "",
          };
          return {
            ...current,
            [payload.purpose]: {
              ...existing,
              roleLabel: payload.role_label || existing.roleLabel,
              reasoning: existing.reasoning + (payload.reasoning || ""),
              content: existing.content + (payload.content || ""),
            },
          };
        });
      } catch {
        // 忽略无法解析的增量
      }
    });
    source.addEventListener("live.call_end", (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent).data) as { purpose: string };
        setLiveCalls((current) => {
          if (!current[payload.purpose]) return current;
          const next = { ...current };
          delete next[payload.purpose];
          return next;
        });
      } catch {
        // 忽略
      }
    });
    source.onerror = () => {
      // EventSource 自动重连；服务重启后内存增量丢失，卡片会随下一次调用恢复
    };
    return () => {
      source.close();
      setLiveCalls({});
    };
  }, [run.id]);

  const budgetPercent = Math.min(100, Math.round((run.budget_used / Math.max(1, run.budget_max)) * 100));
  const contextPercent = run.context
    ? Math.min(
        100,
        Math.round(
          (run.context.estimated_tokens / Math.max(1, run.context.budget_tokens)) * 100,
        ),
      )
    : 0;
  const currentPhase = phaseLabel(run.phase);
  const isBusy = run.status === "RUNNING";
  const pendingInputs = run.pending_inputs ?? [];
  const budgetChanges = run.budget_changes ?? [];
  const canSendInput = ["RUNNING", "PAUSED", "BUDGET_EXHAUSTED"].includes(run.status);
  const isCompleted = ["COMPLETED", "COMPLETED_WITH_WARNINGS"].includes(run.status);
  const currentIdea = run.ideas.find((idea) => idea.id === selectedIdeaId) ?? run.ideas[0];
  const canRefine = isCompleted && currentIdea && currentIdea.current_version > 0;
  const canExtendBudget = canSendInput && run.budget_max < maxExtendedCalls;
  const validBudget = Number.isInteger(nextBudget) && nextBudget > run.budget_max && nextBudget <= maxExtendedCalls;

  // 一次模型调用会先落一条 thinking、再落一条正式发言（多模型面板为多条 thinking
  // 对应一条综合发言）。渲染时把连续的 thinking 与其后的一条正式发言合并为一张卡片。
  type RenderItem =
    | { kind: "merged"; thinking: Message[]; message: Message }
    | { kind: "single"; message: Message };
  const renderItems = useMemo<RenderItem[]>(() => {
    const visible = hideThinking
      ? run.messages.filter((message) => message.message_type !== "thinking")
      : run.messages;
    const items: RenderItem[] = [];
    let pending: Message[] = [];
    for (const message of visible) {
      if (message.message_type === "thinking") {
        pending.push(message);
        continue;
      }
      if (pending.length > 0) {
        items.push({ kind: "merged", thinking: pending, message });
        pending = [];
      } else {
        items.push({ kind: "single", message });
      }
    }
    for (const message of pending) {
      items.push({ kind: "single", message });
    }
    return items;
  }, [run.messages, hideThinking]);
  const routes = Array.isArray(run.state.candidates)
    ? (run.state.candidates as Array<{ id: string; title: string; parent_ids?: string[]; status?: string; hypothesis_delta?: string; discriminating_test?: string }>)
    : [];
  const evolvedRoutes = routes.filter((route) => route.parent_ids?.length);
  const selectedRoutes = Array.isArray(run.state.selected)
    ? (run.state.selected as Array<{ candidate_id: string; title: string; validation_card?: unknown }>)
    : [];
  const objections = Array.isArray(run.state.objections)
    ? (run.state.objections as Array<Record<string, string>>)
    : [];
  const pendingObjections = objections.filter((item) =>
    ["open", "contested", "unresolved"].includes(item.status || "open"),
  );
  const literatureCount = run.messages.filter((m) => m.message_type === "literature").length;
  const panelCount = run.messages.filter((m) => m.message_type === "panel").length;
  const debateCount = run.messages.filter((m) => m.message_type === "debate").length;

  async function sendInput() {
    if (!canSendInput || sending || !input.trim()) return;
    setSending(true);
    setActionError("");
    try {
      await api.addRunInput(run.id, {
        kind: inputKind,
        content: input.trim(),
      });
      setInput("");
      onRefresh();
    } catch (exc) {
      setActionError(exc instanceof Error ? exc.message : "输入失败");
      onRefresh();
    } finally {
      setSending(false);
    }
  }

  async function control(action: "pause" | "resume" | "cancel") {
    setActionError("");
    try {
      if (action === "pause") await api.pauseRun(run.id);
      if (action === "resume") await api.resumeRun(run.id);
      if (action === "cancel") await api.cancelRun(run.id);
      onRefresh();
    } catch (exc) {
      setActionError(exc instanceof Error ? exc.message : "操作失败");
    }
  }

  function openBudget() {
    setNextBudget(Math.min(run.budget_max + 10, maxExtendedCalls));
    setBudgetError("");
    setShowBudget(true);
  }

  async function extendBudget() {
    if (!canExtendBudget || !validBudget || savingBudget) return;
    setBudgetError("");
    setSavingBudget(true);
    try {
      await api.updateBudget(run.id, nextBudget);
      setShowBudget(false);
      onRefresh();
    } catch (exc) {
      setBudgetError(exc instanceof Error ? exc.message : "预算更新失败");
      onRefresh();
    } finally {
      setSavingBudget(false);
    }
  }

  return (
    <div className={`discussion-shell ${focusMode ? "focus-mode" : ""}`}>
      <div className="run-ribbon">
        <div className="phase-track">
          {phases.map((phase, index) => (
            <div
              key={phase.label}
              className={`phase-item ${index < activePhaseIndex ? "done" : ""} ${
                index === activePhaseIndex ? "active" : ""
              }`}
            >
              <span className="phase-index">{String(index + 1).padStart(2, "0")}</span>
              <span>{phase.label}</span>
            </div>
          ))}
        </div>
        <div className="run-ribbon-meta">
          <span className="phase-current">
            <span className={`status-dot ${statusTone(run.status)}`} />
            {statusLabel(run.status)}{run.phase !== "DONE" && ` · ${currentPhase}`}
          </span>
          <div className="ribbon-stats">
            <span className="stat-chip" title="讨论中广播的文献检索消息数">
              文献 {literatureCount}
            </span>
            <span
              className={`stat-chip ${pendingObjections.length > 0 ? "warn" : ""}`}
              title={`累计反驳 ${objections.length} 条，未决 ${pendingObjections.length} 条；辩论相关消息 ${debateCount} 条`}
            >
              反驳 {objections.length}
            </span>
            <span className="stat-chip" title="多模型面板提案消息数">
              提案 {panelCount}
            </span>
          </div>
          <div className="budget-meter" title={`${run.budget_used} / ${run.budget_max} 次调用`}>
            <div className="budget-meter-head">
              <span>调用预算</span>
              <strong>
                {run.budget_used} / {run.budget_max}
              </strong>
            </div>
            <div className="progress-bar">
              <span style={{ width: `${budgetPercent}%` }} />
            </div>
          </div>
          {(canExtendBudget || budgetChanges.length > 0) && (
            <button className="button ghost small" onClick={openBudget}>
              {canExtendBudget && <Icon name="plus" size={14} />}
              {canExtendBudget ? "增加预算" : "预算记录"}
            </button>
          )}
          {run.context && (
            <div
              className="budget-meter context-meter"
              title={`最近一次请求：估算 ${run.context.estimated_tokens} / 预算 ${run.context.budget_tokens} token；实际输入 ${run.context.actual_prompt_tokens || "—"}；已压缩 ${run.context_stats?.compressions || 0} 次`}
            >
              <div className="budget-meter-head">
                <span>上下文</span>
                <strong>
                  {formatTokens(run.context.estimated_tokens)} / {formatTokens(run.context.budget_tokens)}
                </strong>
              </div>
              <div className="progress-bar">
                <span style={{ width: `${contextPercent}%` }} />
              </div>
            </div>
          )}
          <button className="button ghost small" onClick={() => setShowCalls(true)}>
            <Icon name="cpu" size={14} /> 用量明细
          </button>
        </div>
      </div>

      {run.error && (
        <div className={`run-alert ${statusTone(run.status)}`}>
          <strong>{statusLabel(run.status)}</strong>
          <span>{run.error}</span>
        </div>
      )}

      <div className="discussion-grid">
        <aside className="discussion-left">
          <section className="rail-card">
            <span className="eyebrow">本次议程</span>
            <p className="agenda-text">
              {String(run.state.agenda || "PI 正在形成研究议程。")}
            </p>
            <button className="text-button" onClick={() => setShowAgenda(true)}>
              查看完整议程与选择标准
            </button>
          </section>

          <section className="rail-card">
            <div className="rail-card-head">
              <span className="eyebrow">已接受决策</span>
              <span className="count-pill">{run.decisions.length}</span>
            </div>
            <div className="decision-list">
              {run.decisions.length === 0 && <p className="muted">尚无决策。</p>}
              {run.decisions.slice(-5).map((decision) => (
                <div className="decision-item" key={decision.id}>
                  <strong>{decision.title}</strong>
                  <p>{decision.content}</p>
                  <small>{formatDate(decision.created_at)}</small>
                </div>
              ))}
            </div>
          </section>

          {evolvedRoutes.length > 0 && (
            <section className="rail-card">
              <div className="rail-card-head">
                <span className="eyebrow">讨论产生的新路线</span>
                <span className="count-pill">{evolvedRoutes.length}</span>
              </div>
              <div className="decision-list">
                {evolvedRoutes.map((route) => (
                  <details key={route.id} className="decision-item">
                    <summary>{route.title}{route.status === "selected" ? " · 已入选" : ""}</summary>
                    <small>源自 {route.parent_ids?.map((id) => routes.find((item) => item.id === id)?.title || id).join("、")}</small>
                    <p>{route.hypothesis_delta}</p>
                    <p>{route.discriminating_test}</p>
                  </details>
                ))}
              </div>
            </section>
          )}

          {selectedRoutes.filter((route) => route.validation_card).map((route) => (
            <section key={route.candidate_id} className="rail-card">
              <strong>{route.title}</strong>
              <ValidationCard value={route.validation_card} />
            </section>
          ))}

          {pendingObjections.length > 0 && (
            <section className="rail-card">
              <div className="rail-card-head">
                <span className="eyebrow">未决反驳</span>
                <span className="count-pill warn">{pendingObjections.length}</span>
              </div>
              <div className="objection-list">
                {pendingObjections.slice(-4).map((item, index) => (
                  <div className="objection-item" key={item.id || index}>
                    <div className="objection-head">
                      <span className={`severity-chip ${item.severity || "major"}`}>
                        {severityLabel(String(item.severity || "major"))}
                      </span>
                      <small>
                        {item.role} → {item.target}
                      </small>
                    </div>
                    <p>{item.issue}</p>
                    {item.fix_direction && (
                      <small className="fix-hint">修复方向：{item.fix_direction}</small>
                    )}
                  </div>
                ))}
                {pendingObjections.length > 4 && (
                  <small className="muted">其余 {pendingObjections.length - 4} 条见讨论记录。</small>
                )}
              </div>
            </section>
          )}

        </aside>

        <main className="discussion-center">
          <div className="discussion-toolbar">
            <div>
              <span className="eyebrow">会议记录</span>
              <h2>{currentPhase}</h2>
            </div>
            <div className="run-controls">
              <button className="button ghost small" onClick={() => setShowAgenda(true)}>
                <Icon name="book" size={14} /> 议程
              </button>
              <button
                className={`button ghost small ${hideThinking ? "active-toggle" : ""}`}
                onClick={() => setHideThinking((value) => !value)}
                title={hideThinking ? "显示思考过程" : "隐藏思考过程，让讨论更清爽"}
              >
                思考{hideThinking ? "已隐藏" : ""}
              </button>
              <button
                className={`button ghost small ${focusMode ? "active-toggle" : ""}`}
                onClick={() => setFocusMode((value) => !value)}
                title={focusMode ? "恢复议程侧栏" : "隐藏左侧议程栏，专注讨论"}
              >
                {focusMode ? "退出专注" : "专注"}
              </button>
              {isBusy && (
                <button className="button ghost small" onClick={() => control("pause")}>
                  <Icon name="pause" size={14} /> 暂停
                </button>
              )}
              {["PAUSED", "FAILED"].includes(run.status) && (
                <button className="button ghost small" onClick={() => control("resume")}>
                  <Icon name="play" size={14} /> 继续
                </button>
              )}
              {!["COMPLETED", "COMPLETED_WITH_WARNINGS", "CANCELLED", "FAILED", "BUDGET_EXHAUSTED"].includes(
                run.status,
              ) && (
                <button className="button ghost small danger-text" onClick={() => control("cancel")}>
                  <Icon name="stop" size={14} /> 终止
                </button>
              )}
            </div>
          </div>

          <DispatchGraph key={run.id} run={run} onJumpToMessage={jumpToMessage} />
          <div className="message-stream" aria-live="polite" ref={messageStream}>
            {run.messages.length === 0 && (
              <div className="waiting-card">
                <span className="pulse-mark" />
                <div>
                  <strong>
                    {run.mode === "REVISE" ? "PI 正在分析用户反馈" : "PI 正在定义问题与议程"}
                  </strong>
                  <p>首个模型调用返回后，讨论会出现在这里。</p>
                </div>
              </div>
            )}
            {renderItems
              .map((item) => {
              if (item.kind === "merged") {
                const message = item.message;
                const typeBadge =
                  message.message_type === "panel" ? (
                    <Badge tone="complete">模型提案</Badge>
                  ) : message.message_type === "debate" ? (
                    <Badge tone="warning">辩论</Badge>
                  ) : message.message_type === "literature" ? (
                    <Badge tone="info">文献检索</Badge>
                  ) : message.message_type === "artifact" ? (
                    <Badge tone="complete">文档更新</Badge>
                  ) : message.message_type === "user_input" ? (
                    <Badge tone="warning">用户输入</Badge>
                  ) : null;
                return (
                  <article className={`message message-${message.role}`} key={`m-${message.id}`} data-message-id={message.id} tabIndex={-1}>
                    <div className="message-avatar">{roleInitial(message.role)}</div>
                    <div className="message-body">
                      <div className="message-meta">
                        <strong>{message.role_label}</strong>
                        <span>{formatDate(message.created_at)}</span>
                        {typeBadge}
                      </div>
                      <details className="thinking-details thinking-inline">
                        <summary>
                          思考过程 · {item.thinking.length} 次调用（仅供查看，不进入上下文）
                        </summary>
                        {item.thinking.map((thought) => (
                          <pre className="thinking-text" key={thought.id}>
                            {thought.content}
                          </pre>
                        ))}
                      </details>
                      <div className="message-content">
                        <MarkdownView content={message.content} />
                      </div>
                    </div>
                  </article>
                );
              }
              const message = item.message;
              if (message.message_type === "thinking") {
                return (
                  <article className="message message-thinking" key={message.id}>
                    <div className="message-avatar">{roleInitial(message.role)}</div>
                    <div className="message-body">
                      <div className="message-meta">
                        <strong>{message.role_label} · 思考过程</strong>
                        <span>{formatDate(message.created_at)}</span>
                        <Badge tone="neutral">仅供查看，不进入上下文</Badge>
                      </div>
                      <details className="thinking-details">
                        <summary>展开 / 收起模型思考过程</summary>
                        <pre className="thinking-text">{message.content}</pre>
                      </details>
                    </div>
                  </article>
                );
              }
              if (message.message_type === "literature") {
                const structured = (message.structured || {}) as {
                  works?: Array<{
                    source?: string;
                    title?: string;
                    year?: number | null;
                    venue?: string;
                    url?: string;
                    abstract?: string;
                  }>;
                };
                const works = Array.isArray(structured.works) ? structured.works : [];
                return (
                  <article className="message message-literature" key={message.id} data-message-id={message.id} tabIndex={-1}>
                    <div className="message-avatar">{roleInitial(message.role)}</div>
                    <div className="message-body">
                      <div className="message-meta">
                        <strong>{message.role_label} · 文献检索</strong>
                        <span>{formatDate(message.created_at)}</span>
                        <Badge tone="info">{works.length} 篇结果</Badge>
                      </div>
                      {works.length > 0 ? (
                        <div className="lit-cards">
                          {works.map((work, index) => (
                            <div className="lit-card" key={index}>
                              <div className="lit-card-head">
                                <span className="source-chip">{work.source || "Web"}</span>
                                <strong title={work.title}>{work.title || "未命名结果"}</strong>
                              </div>
                              <small>
                                {work.year || "年份未知"} ·{" "}
                                {work.venue || (work.source?.startsWith("Web") ? "网页" : "venue 未知")}
                              </small>
                              {work.abstract && (
                                <p>
                                  {String(work.abstract).slice(0, 220)}
                                  {String(work.abstract).length > 220 ? "…" : ""}
                                </p>
                              )}
                              {work.url && (
                                <a href={work.url} target="_blank" rel="noreferrer">
                                  打开原文
                                </a>
                              )}
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="message-content">
                          <MarkdownView content={message.content} />
                        </div>
                      )}
                    </div>
                  </article>
                );
              }
              const typeBadge =
                message.message_type === "panel" ? (
                  <Badge tone="complete">模型提案</Badge>
                ) : message.message_type === "debate" ? (
                  <Badge tone="warning">辩论</Badge>
                ) : message.message_type === "literature" ? (
                  <Badge tone="info">文献检索</Badge>
                ) : message.message_type === "artifact" ? (
                  <Badge tone="complete">文档更新</Badge>
                ) : message.message_type === "user_input" ? (
                  <Badge tone="warning">用户输入</Badge>
                ) : null;
              return (
                <article className={`message message-${message.role}`} key={message.id} data-message-id={message.id} tabIndex={-1}>
                  <div className="message-avatar">{roleInitial(message.role)}</div>
                  <div className="message-body">
                    <div className="message-meta">
                      <strong>{message.role_label}</strong>
                      <span>{formatDate(message.created_at)}</span>
                      {typeBadge}
                    </div>
                    <div className="message-content">
                      <MarkdownView content={message.content} />
                    </div>
                  </div>
                </article>
              );
            })}
            {Object.values(liveCalls).map((call) => {
              const jsonView = extractLiveSpeech(call.content);
              const speechText = jsonView ? unescapeJsonText(jsonView.text) : "";
              return (
              <article className="message message-live" key={`live-${call.purpose}`}>
                <div className="message-avatar">{roleInitial(call.roleLabel || "pi")}</div>
                <div className="message-body">
                  <div className="message-meta">
                    <strong>{call.roleLabel || "模型"} · 正在生成</strong>
                    <span className="live-chip">
                      <span className="pulse-mark" />
                      流式输出
                    </span>
                  </div>
                  <small className="live-purpose">{call.purpose}</small>
                  {call.reasoning && (
                    <details className="thinking-details thinking-inline thinking-live" open>
                      <summary>思考过程（实时 · {call.reasoning.length} 字符）</summary>
                      <LiveThinkingPre text={call.reasoning} />
                    </details>
                  )}
                  {call.content && (
                    <div className="message-content">
                      {jsonView ? (
                        speechText ? (
                          <MarkdownView
                            content={
                              speechText.length > 4000
                                ? `${speechText.slice(0, 4000)}\n\n…（生成中）`
                                : speechText
                            }
                          />
                        ) : (
                          <div className="live-json-note">
                            结构化字段生成中（已输出 {call.content.length} 字符），
                            正文 speech 字段出现后此处实时显示。
                          </div>
                        )
                      ) : (
                        <MarkdownView
                          content={
                            call.content.length > 4000
                              ? `${call.content.slice(0, 4000)}\n\n…（生成中）`
                              : call.content
                          }
                        />
                      )}
                    </div>
                  )}
                  {!call.reasoning && !call.content && (
                    <div className="thinking-row">
                      <span className="pulse-mark" />
                      <span>等待首个增量…</span>
                    </div>
                  )}
                </div>
              </article>
              );
            })}
            {isBusy && (
              <div className="thinking-row">
                <span className="pulse-mark" />
                <span>团队正在处理「{currentPhase}」的当前议程，完成一次模型调用后更新。</span>
              </div>
            )}
            <div ref={messageEnd} />
          </div>

          {showJumpLatest && (
            <button className="jump-latest" onClick={jumpToLatest} title="回到最新消息">
              <Icon name="chevron" size={14} /> 回到最新
            </button>
          )}

          {(canSendInput || canRefine || pendingInputs.length > 0 || actionError) && <div className="composer">
            {actionError && <p className="form-error" role="alert">{actionError}</p>}
            {pendingInputs.length > 0 && (
              <details className="pending-inputs">
                <summary>
                  {canSendInput ? "等待进入模型请求" : "运行结束时尚未进入模型请求"} · {pendingInputs.length} 条
                </summary>
                <ul>
                  {pendingInputs.map((entry) => (
                    <li key={entry.id}><strong>{inputLabels[entry.kind]} </strong>{entry.content}</li>
                  ))}
                </ul>
              </details>
            )}
            {canSendInput && <>
              <div className="composer-head">
                <select
                  value={inputKind}
                  onChange={(event) => setInputKind(event.target.value)}
                  disabled={sending}
                  aria-label="输入类型"
                >
                  {Object.entries(inputLabels).map(([kind, label]) => (
                    <option key={kind} value={kind}>{label}</option>
                  ))}
                </select>
                <span>{isBusy ? "输入会在下一次模型请求前加入上下文" : "恢复运行后，输入会加入模型上下文"}</span>
              </div>
              <textarea
                rows={2}
                value={input}
                onChange={(event) => setInput(event.target.value)}
                disabled={sending}
                maxLength={12000}
                aria-label="讨论输入"
                placeholder="例如：不要继续扩展模块数量，重点解释机制在什么条件下失效。"
                onKeyDown={(event) => {
                  if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    void sendInput();
                  }
                }}
              />
              <div className="composer-actions">
                <span className="composer-hint">Ctrl / ⌘ + Enter</span>
                <button className="button primary small" onClick={sendInput} disabled={sending || !input.trim()}>
                  <Icon name="send" size={14} /> {sending ? "发送中" : "加入讨论"}
                </button>
              </div>
            </>}
            {canRefine && (
              <div className="composer-actions">
                <span className="composer-hint" title={currentIdea.title}>
                  补充实验结果或继续修订当前 Idea
                </span>
                <button className="button primary small" onClick={() => setFeedbackIdea(currentIdea)}>
                  <Icon name="send" size={14} /> 继续讨论
                </button>
              </div>
            )}
          </div>}
        </main>

        <DocumentPane
          run={run}
          refreshKey={refreshKey}
          selectedIdeaId={selectedIdeaId}
          onSelectedIdeaChange={setSelectedIdeaId}
        />
      </div>

      {showCalls && <CallsModal runId={run.id} onClose={() => setShowCalls(false)} />}
      {feedbackIdea && (
        <FeedbackModal
          idea={feedbackIdea}
          onClose={() => setFeedbackIdea(null)}
          onCreated={(runId) => {
            setFeedbackIdea(null);
            onFeedbackCreated(runId);
          }}
        />
      )}
      {showBudget && (
        <Modal
          title={canExtendBudget ? "增加本次运行的调用预算" : "调用预算记录"}
          subtitle={`已使用 ${run.budget_used} / ${run.budget_max} 次调用。`}
          onClose={() => setShowBudget(false)}
        >
          <div className="form-stack">
            {budgetChanges.length > 0 && (
              <ol className="budget-history" aria-label="预算变更记录">
                {budgetChanges.map((change) => (
                  <li key={change.id}>
                    <strong>{change.previous_max} → {change.max} 次</strong>
                    <span>{formatDate(change.created_at)} · 当时已用 {change.used} 次</span>
                  </li>
                ))}
              </ol>
            )}
            {canExtendBudget && <>
              <label className="field compact">
                <span>新的最大调用次数</span>
                <input
                  type="number"
                  min={run.budget_max + 1}
                  max={maxExtendedCalls}
                  value={nextBudget}
                  onChange={(event) => setNextBudget(Number(event.target.value))}
                  disabled={savingBudget}
                />
              </label>
              {run.status === "PAUSED" && <p className="muted">保存后保持暂停，可手动继续。</p>}
            </>}
            {budgetError && <p className="form-error" role="alert">{budgetError}</p>}
            <div className="modal-actions">
              <button className="button ghost" onClick={() => setShowBudget(false)}>{canExtendBudget ? "取消" : "关闭"}</button>
              {canExtendBudget && (
                <button className="button primary" onClick={extendBudget} disabled={!validBudget || savingBudget}>
                  {savingBudget ? "保存中" : run.status === "BUDGET_EXHAUSTED" ? "增加并继续" : "增加预算"}
                </button>
              )}
            </div>
          </div>
        </Modal>
      )}
      {showAgenda && (
        <Modal title="议程与选择标准" onClose={() => setShowAgenda(false)} wide>
          <div className="agenda-modal">
            <MarkdownView content={String(run.state.agenda || "尚未形成议程。")} />
            <h3>选择标准</h3>
            <ul>
              {(Array.isArray(run.state.selection_criteria)
                ? (run.state.selection_criteria as string[])
                : []
              ).map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
            {typeof run.state.problem_frame === "string" && (
              <>
                <h3>问题边界</h3>
                <p>{run.state.problem_frame}</p>
              </>
            )}
            <h3>已接受决策</h3>
            {run.decisions.length === 0 ? (
              <p className="muted">尚无决策。</p>
            ) : (
              <div className="decision-list">
                {run.decisions.map((decision) => (
                  <div className="decision-item" key={decision.id}>
                    <strong>{decision.title}</strong>
                    <p>{decision.content}</p>
                    {decision.rationale && <small>{decision.rationale}</small>}
                  </div>
                ))}
              </div>
            )}
          </div>
        </Modal>
      )}
    </div>
  );
}

function CallsModal({ runId, onClose }: { runId: string; onClose: () => void }) {
  const [summary, setSummary] = useState<CallSummary | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api
      .getCalls(runId)
      .then(setSummary)
      .catch((exc) => setError(exc instanceof Error ? exc.message : "加载失败"));
  }, [runId]);
  return (
    <Modal title="模型调用明细" subtitle="预算按供应商请求计数，包含失败重试与 JSON 修复。" onClose={onClose} wide>
      {error && <p className="form-error">{error}</p>}
      {summary && (
        <>
          <div className="usage-summary">
            <div>
              <span>调用</span>
              <strong>{summary.totals.calls}</strong>
            </div>
            <div>
              <span>输入 Token</span>
              <strong>{summary.totals.prompt_tokens.toLocaleString()}</strong>
            </div>
            <div>
              <span>输出 Token</span>
              <strong>{summary.totals.completion_tokens.toLocaleString()}</strong>
            </div>
            <div>
              <span>估算成本</span>
              <strong>${summary.totals.cost_usd.toFixed(4)}</strong>
            </div>
          </div>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>角色</th>
                  <th>用途</th>
                  <th>状态</th>
                  <th>输入</th>
                  <th>输出</th>
                  <th>耗时</th>
                </tr>
              </thead>
              <tbody>
                {summary.calls.map((call) => (
                  <tr key={call.id}>
                    <td>{call.role}</td>
                    <td>{call.purpose}</td>
                    <td title={call.error || undefined}>
                      <Badge tone={call.status === "success" ? "positive" : "danger"}>
                        {call.status === "invalid_output" ? "输出校验失败" : call.status === "timed_out" ? "请求超时" : call.status}
                      </Badge>
                    </td>
                    <td>{call.prompt_tokens.toLocaleString()}</td>
                    <td>{call.completion_tokens.toLocaleString()}</td>
                    <td>{call.latency_ms ? `${(call.latency_ms / 1000).toFixed(1)}s` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Modal>
  );
}

export function IdeaStrip({ ideas }: { ideas: Idea[] }) {
  if (!ideas.length) return null;
  return (
    <div className="idea-strip">
      {ideas.map((idea) => (
        <div key={idea.id} className="idea-strip-item">
          <span>{idea.title}</span>
          <Badge tone={statusTone(idea.status)}>{statusLabel(idea.status)}</Badge>
        </div>
      ))}
    </div>
  );
}
