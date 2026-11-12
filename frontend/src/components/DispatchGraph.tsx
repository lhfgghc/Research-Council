import { useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Message, RunDetail } from "../types";
import { phaseLabel, statusLabel } from "../utils";
import { Icon } from "./Icons";

interface Seat {
  key: string;
  role: string;
  label: string;
  role_label: string;
  model_label: string;
  tool?: boolean;
}

interface Dispatch {
  id: string;
  stage: string;
  purpose: string;
  role: string;
  status: string;
  roster: Seat[];
  target_key: string;
  type: string;
  turn?: number | null;
  idea_id?: string;
  source_key?: string;
  requested?: boolean;
  actual_model?: string;
  fallback?: boolean;
  message_id?: number | null;
  reason?: string;
  issue_id?: string;
  outcome?: string;
}

interface Board {
  active?: Dispatch | null;
  last?: Dispatch | null;
  history?: Dispatch[];
}

const floorLabels: Record<string, string> = { "P2.1": "团队讨论", "P2.2": "路线取舍", "P2.3": "单 Idea 辩论", "P2.4": "研究决策" };
const actionLabels: Record<string, string> = {
  CONTINUE: "参与与补位", RESPOND: "回应", CHALLENGE: "挑战",
  EXPLORE: "探查候选", DEVELOP: "完善方案", COMPARE: "比较", REVISE: "修订", RETRIEVE: "检索", CALL: "执行",
};
const actionStates: Record<string, string> = {
  scheduled: "已调度", waiting: "等待首个输出", thinking: "思考中", streaming: "发言中",
  processing: "整理本轮输出", retrieving: "检索中", completed: "已完成", interrupted: "已中断",
};
const emptyRoster: Seat[] = [];

function legacyTurns(messages: Message[], tracked: Dispatch[]): Dispatch[] {
  const linked = new Set(tracked.map((action) => action.message_id));
  const rosters = new Map<string, Map<string, Seat>>();
  const turns = [];
  for (const message of messages) {
    const scheduler = message.structured?.scheduler as Record<string, unknown> | undefined;
    if (!scheduler || typeof scheduler.target_key !== "string" || !scheduler.target_key) continue;
    const debate = message.message_type === "debate";
    const stage = debate ? "P2.3" : "P2.1";
    const scope = debate ? message.scope || "debate" : "team";
    const model = typeof message.structured?.actual_model === "string" ? message.structured.actual_model : "";
    const key = scheduler.target_key;
    const modelLabel = debate ? message.content.match(/^【(.+?) · /)?.[1] || model : "";
    const seats = rosters.get(scope) ?? new Map<string, Seat>();
    rosters.set(scope, seats);
    seats.set(key, { key, role: message.role, label: modelLabel || message.role_label, role_label: message.role_label, model_label: model });
    if (!linked.has(message.id)) {
      turns.push({
        ...scheduler, id: `message-${message.id}`, stage, purpose: actionLabels[String(scheduler.type)] || "",
        role: message.role, type: String(scheduler.type || "CALL"), target_key: key,
        turn: Number(message.structured?.turn) || null, status: "completed",
        idea_id: debate && scope.startsWith("P2:") ? scope.slice(3) : String(scheduler.idea_id || ""),
        actual_model: model, message_id: message.id, scope,
      });
    }
  }
  return turns.map(({ scope, ...turn }) => ({ ...turn, roster: [...(rosters.get(scope)?.values() ?? [])] }));
}

interface Connector {
  key: string;
  inbound: boolean;
  active: boolean;
  d: string;
}

export function DispatchGraph({ run, onJumpToMessage }: { run: RunDetail; onJumpToMessage: (id: number) => void }) {
  const board = run.state.dispatch as Board | undefined;
  const history = useMemo(() => {
    const tracked = board?.history ?? [];
    const older = legacyTurns(run.messages, tracked);
    return [...older, ...tracked];
  }, [board?.history, run.messages]);
  const [selection, setSelection] = useState("live");
  const chosen = history.find((item) => item.id === selection);
  const live = run.status === "RUNNING" ? board?.active : null;
  const current = chosen ?? live ?? board?.last ?? history[history.length - 1];
  const followingLive = Boolean(live && !chosen);
  const roster = current?.roster ?? emptyRoster;
  const target = roster.find((seat) => seat.key === current?.target_key);
  const origin = roster.find((seat) => seat.key === current?.source_key && seat.key !== target?.key);
  const seats = roster.filter((seat) => !seat.tool);
  const roleCount = new Set(seats.map((seat) => seat.role)).size;
  const ideaTitle = current?.idea_id && (
    run.ideas.find((idea) => idea.id === current.idea_id)?.title ||
    (Array.isArray(run.state.candidates) ? run.state.candidates as { id?: string; title?: string }[] : [])
      .find((idea) => idea.id === current.idea_id)?.title
  );
  const stage = (chosen || live) && current ? floorLabels[current.stage] || phaseLabel(current.stage) : phaseLabel(run.phase);
  const state = chosen ? actionStates[chosen.status] : live ? actionStates[live.status]
    : run.status === "RUNNING" ? "等待调度" : statusLabel(run.status);
  const targetStatus = followingLive ? state : current ? actionStates[current.status] : state;
  const markerId = useId();
  const graph = useRef<HTMLDivElement>(null);
  const [geometry, setGeometry] = useState({ width: 0, height: 0, paths: [] as Connector[] });

  useLayoutEffect(() => {
    const element = graph.current;
    if (!element) return;
    const hub = element.querySelector("[data-dispatch-hub]");
    if (!hub) return;
    const endpoints = [...element.querySelectorAll<HTMLElement>("[data-seat-key]")];
    function measure() {
      if (!element || !hub) return;
      const bounds = element.getBoundingClientRect();
      const start = hub.getBoundingClientRect();
      const x = start.left + start.width / 2 - bounds.left;
      const y = start.bottom - bounds.top;
      const paths = endpoints.map((node) => {
        const end = node.getBoundingClientRect();
        const tx = end.left + end.width / 2 - bounds.left;
        const ty = end.top - bounds.top - 2;
        const mid = y + (ty - y) * .5;
        const inbound = node.dataset.seatKey === origin?.key;
        return {
          key: node.dataset.seatKey || "", inbound, active: node.dataset.seatKey === target?.key,
          d: inbound ? `M ${tx} ${ty} C ${tx} ${mid}, ${x} ${mid}, ${x} ${y + 2}`
            : `M ${x} ${y} C ${x} ${mid}, ${tx} ${mid}, ${tx} ${ty}`,
        };
      });
      setGeometry({ width: bounds.width, height: bounds.height, paths });
    }
    const observer = new ResizeObserver(measure);
    [element, hub, ...endpoints].forEach((node) => observer.observe(node));
    measure();
    return () => observer.disconnect();
  }, [roster, target?.key, origin?.key]);

  function messageFor(seat: Seat) {
    if (seat.key === target?.key) return current?.message_id;
    for (let index = history.length - 1; index >= 0; index -= 1) {
      const item = history[index];
      if (item.message_id && item.target_key === seat.key && item.stage === current?.stage
        && item.idea_id === current?.idea_id && item.message_id < (current?.message_id ?? Infinity)) return item.message_id;
    }
  }

  return (
    <details className="dispatch-panel" open>
      <summary className="dispatch-summary">
        <span className="dispatch-title">调度图 <small>{stage}</small></span>
        <span className="dispatch-summary-meta">
          <span>{live && !chosen ? `${target?.label || live.role} · ${state}` : state}</span>
          {roster.length > 0 && <span>{seats.length > 0 && seats.every((seat) => seat.key.startsWith("model|")) ? `${seats.length} 个模型` : `${roleCount} 角色 · ${seats.length} 席位`}</span>}
          <Icon name="chevron" size={13} />
        </span>
      </summary>
      <div className="dispatch-body">
        {history.length > 0 && (
          <label className="dispatch-history">
            <span>轮次</span>
            <select aria-label="调度轮次" value={chosen?.id ?? "live"} onChange={(event) => setSelection(event.target.value)}>
              <option value="live">实时跟随</option>
              {[...history].reverse().map((item) => (
                <option value={item.id} key={item.id}>
                  {item.stage} {item.turn ? `第 ${item.turn} 轮` : actionLabels[item.type]} · {item.roster.find((seat) => seat.key === item.target_key)?.label || item.role}{item.issue_id ? ` · ${item.issue_id}` : ""}
                </option>
              ))}
            </select>
          </label>
        )}
        {roster.length ? (
          <div className={`dispatch-canvas${followingLive ? " is-live" : ""}`} ref={graph}>
            <div className="dispatch-hub" data-dispatch-hub>{current?.stage === "P2.1" || current?.stage === "P2.3" ? "调度器" : "工作流"}</div>
            {geometry.width > 0 && geometry.height > 0 && (
              <svg className="dispatch-edges" viewBox={`0 0 ${geometry.width} ${geometry.height}`} aria-hidden="true">
                <defs>
                  <marker id={markerId} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
                    <path d="M0 0 L8 4 L0 8 Z" className="dispatch-arrowhead" />
                  </marker>
                  <marker id={`${markerId}-source`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
                    <path d="M0 0 L8 4 L0 8 Z" className="dispatch-arrowhead is-source" />
                  </marker>
                </defs>
                {geometry.paths.map((edge) => (
                  <g key={edge.key}>
                    <path d={edge.d} className={`dispatch-edge${edge.active ? " is-target" : ""}${edge.inbound ? " is-source" : ""}`}
                      markerEnd={edge.inbound ? `url(#${markerId}-source)` : edge.active ? `url(#${markerId})` : undefined} />
                    {edge.active && current?.message_id && (
                      <path d={edge.d} className="dispatch-edge-hit" onClick={() => onJumpToMessage(current.message_id!)} />
                    )}
                  </g>
                ))}
              </svg>
            )}
            <div className="dispatch-seats">
              {roster.map((seat) => {
                const selected = seat.key === target?.key;
                const messageId = messageFor(seat);
                return (
                  <button type="button" key={seat.key} data-seat-key={seat.key}
                    className={`dispatch-seat${selected ? " is-target" : ""}${seat.key === origin?.key ? " is-source" : ""}`}
                    disabled={!messageId} onClick={() => messageId && onJumpToMessage(messageId)}
                    title={messageId ? `定位 ${seat.label} 的消息` : seat.label}>
                    <strong>{seat.label}</strong>
                    <span>{selected ? targetStatus : seat.key === origin?.key ? current?.requested ? "发起点名" : "提出异议" : seat.model_label && seat.model_label !== seat.label ? seat.model_label : seat.role_label !== seat.label ? seat.role_label : followingLive ? "等待调度" : "本阶段席位"}</span>
                  </button>
                );
              })}
            </div>
          </div>
        ) : <p className="dispatch-empty" aria-live="polite">{state}</p>}
        {current && (
          <div className="dispatch-detail" aria-live="polite">
            <div>
              <strong>{target?.label || current.role} · {actionLabels[current.type] || current.type}</strong>
              {current.issue_id && <code>{current.issue_id}</code>}
              {ideaTitle && <span className="dispatch-idea" title={ideaTitle}>{ideaTitle}</span>}
              <small>{current.reason || current.purpose}{current.outcome ? ` · ${current.outcome}` : ""}</small>
              {(current.actual_model || target?.model_label) && (
                <small>{current.fallback ? "备用模型 " : current.actual_model ? "实际模型 " : "计划模型 "}{current.actual_model || target?.model_label}</small>
              )}
            </div>
            {current.message_id && <button type="button" className="text-button" onClick={() => onJumpToMessage(current.message_id!)}>定位消息</button>}
          </div>
        )}
      </div>
    </details>
  );
}
