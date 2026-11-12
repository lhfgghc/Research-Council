import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Idea, ProjectDetail, RunSummary } from "../types";
import { formatDate, statusLabel, statusTone, truncate } from "../utils";
import { MarkdownView } from "./DocumentPane";
import { FeedbackModal } from "./FeedbackModal";
import { Icon } from "./Icons";
import { Badge, EmptyState, Modal } from "./UI";
import { ValidationCard } from "./ValidationCard";

export function IdeaWorkspace({
  project,
  refreshKey,
  onFeedbackCreated,
  onStartRun,
}: {
  project: ProjectDetail;
  refreshKey: number;
  onFeedbackCreated: (runId: string) => void;
  onStartRun: () => void;
}) {
  const [selectedIdea, setSelectedIdea] = useState<Idea | null>(null);
  const [feedbackIdea, setFeedbackIdea] = useState<Idea | null>(null);

  const grouped = useMemo(() => {
    const map = new Map<string, RunSummary>();
    project.runs.forEach((run) => map.set(run.id, run));
    return project.ideas.map((idea) => ({
      idea,
      run: map.get(idea.run_id),
    }));
  }, [project.ideas, project.runs]);

  if (project.ideas.length === 0) {
    return (
      <div className="library-empty">
        <EmptyState
          title="还没有完整 Idea"
          description="启动一次讨论后，入选路线的研究方案和首轮验证会保存在这里。"
          action={
            <button className="button primary" onClick={onStartRun}>
              <Icon name="play" size={16} /> 启动讨论
            </button>
          }
        />
      </div>
    );
  }

  return (
    <div className="idea-library">
      <div className="library-head">
        <div>
          <span className="eyebrow">Idea 资产库</span>
          <h2>{project.ideas.length} 份研究方案</h2>
          <p>每份方案保留生成版本、修订说明与证据状态，不以聊天记录代替正式文档。</p>
        </div>
        <button className="button ghost" onClick={onStartRun}>
          <Icon name="plus" size={16} /> 新增一轮讨论
        </button>
      </div>

      <div className="idea-grid">
        {grouped.map(({ idea, run }, index) => (
          <article className="idea-card" key={idea.id}>
            <div className="idea-card-top">
              <span className="idea-number">{String(index + 1).padStart(2, "0")}</span>
              <Badge tone={statusTone(idea.status)}>{statusLabel(idea.status)}</Badge>
            </div>
            <h3>{idea.title}</h3>
            <p>{truncate(idea.summary || "暂无摘要", 220)}</p>
            <div className="idea-card-meta">
              <span>v{String(idea.current_version).padStart(3, "0")}</span>
              <span>{run?.mode === "REVISE" ? "来自后续修订" : "从讨论生成"}</span>
              <span>{formatDate(idea.updated_at)}</span>
            </div>
            <div className="idea-card-actions">
              <button className="button ghost small" onClick={() => setSelectedIdea(idea)}>
                <Icon name="book" size={15} /> 阅读全文
              </button>
              <button className="button ghost small" onClick={() => setFeedbackIdea(idea)}>
                <Icon name="beaker" size={15} /> 继续修订
              </button>
            </div>
          </article>
        ))}
      </div>

      {project.runs.some((run) => run.mode === "REVISE") && (
        <section className="revision-history">
          <span className="eyebrow">修订历史</span>
          <div className="revision-list">
            {project.runs
              .filter((run) => run.mode === "REVISE")
              .map((run) => (
                <div key={run.id} className="revision-row">
                  <Icon name="refresh" size={16} />
                  <div>
                    <strong>{run.phase_label}</strong>
                    <small>
                      {statusLabel(run.status)} · {formatDate(run.created_at)}
                    </small>
                  </div>
                  <Badge tone={statusTone(run.status)}>{statusLabel(run.status)}</Badge>
                </div>
              ))}
          </div>
        </section>
      )}

      {selectedIdea && (
        <IdeaReaderModal
          idea={selectedIdea}
          refreshKey={refreshKey}
          onClose={() => setSelectedIdea(null)}
          onFeedback={() => {
            setFeedbackIdea(selectedIdea);
            setSelectedIdea(null);
          }}
        />
      )}
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
    </div>
  );
}

function IdeaReaderModal({
  idea,
  refreshKey,
  onClose,
  onFeedback,
}: {
  idea: Idea;
  refreshKey: number;
  onClose: () => void;
  onFeedback: () => void;
}) {
  const [detail, setDetail] = useState<Idea | null>(null);
  const [version, setVersion] = useState(idea.current_version);
  const [documentText, setDocumentText] = useState("");
  const [revisionText, setRevisionText] = useState("");
  const [view, setView] = useState<"document" | "revision" | "compare">("document");
  const [compareA, setCompareA] = useState(Math.max(1, idea.current_version - 1));
  const [compareB, setCompareB] = useState(idea.current_version);
  const [compareTextA, setCompareTextA] = useState("");
  const [compareTextB, setCompareTextB] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .getIdea(idea.id)
      .then((payload) => {
        setDetail(payload);
        setVersion(payload.current_version);
        setDocumentText(payload.document || "");
        setRevisionText(payload.revision_document || "");
      })
      .finally(() => setLoading(false));
  }, [idea.id, refreshKey]);

  useEffect(() => {
    if (!detail || view !== "document") return;
    if (version === detail.current_version) return;
    setLoading(true);
    api
      .getIdeaVersion(detail.id, version)
      .then((payload) => {
        setDocumentText(payload.document);
        setRevisionText(payload.revision_document);
      })
      .finally(() => setLoading(false));
  }, [version, detail?.id, detail?.current_version, view]);

  useEffect(() => {
    if (!detail || view !== "compare") return;
    setLoading(true);
    Promise.all([
      api.getIdeaVersion(detail.id, compareA),
      api.getIdeaVersion(detail.id, compareB),
    ])
      .then(([a, b]) => {
        setCompareTextA(a.document);
        setCompareTextB(b.document);
      })
      .finally(() => setLoading(false));
  }, [view, compareA, compareB, detail?.id]);

  const versions = detail?.versions || idea.versions;
  const manifest = versions.find((item) => item.version === version)?.manifest;
  return (
    <Modal
      title={idea.title}
      subtitle={`${versions.length} 个版本 · 当前 v${String(idea.current_version).padStart(3, "0")}`}
      onClose={onClose}
      wide
    >
      <div className="reader-toolbar">
        <div className="segmented">
          <button className={view === "document" ? "active" : ""} onClick={() => setView("document")}>
            方案正文
          </button>
          <button className={view === "revision" ? "active" : ""} onClick={() => setView("revision")}>
            修订说明
          </button>
          <button className={view === "compare" ? "active" : ""} onClick={() => setView("compare")}>
            版本对比
          </button>
        </div>
        <div className="reader-actions">
          <Badge tone={statusTone(idea.status)}>{statusLabel(idea.status)}</Badge>
          {view !== "compare" && (
            <select value={version} onChange={(event) => setVersion(Number(event.target.value))}>
              {versions.map((item) => (
                <option key={item.version} value={item.version}>
                  v{String(item.version).padStart(3, "0")}
                </option>
              ))}
            </select>
          )}
          <button className="button primary small" onClick={onFeedback}>
            <Icon name="beaker" size={15} /> 继续修订
          </button>
        </div>
      </div>

      {loading && <div className="skeleton-lines"><span /><span /><span /><span /></div>}

      {!loading && view === "document" && (
        <div className="reader-scroll">
          {manifest?.deliverable !== "idea_brief" && <ValidationCard value={manifest?.validation_card} />}
          <MarkdownView content={documentText || "该版本尚无正文。"} />
        </div>
      )}
      {!loading && view === "revision" && (
        <div className="reader-scroll">
          <MarkdownView content={revisionText || "该版本没有修订说明。"} />
        </div>
      )}
      {!loading && view === "compare" && (
        <div className="compare-toolbar">
          <label>
            左：
            <select value={compareA} onChange={(event) => setCompareA(Number(event.target.value))}>
              {versions.map((item) => (
                <option key={item.version} value={item.version}>
                  v{String(item.version).padStart(3, "0")}
                </option>
              ))}
            </select>
          </label>
          <label>
            右：
            <select value={compareB} onChange={(event) => setCompareB(Number(event.target.value))}>
              {versions.map((item) => (
                <option key={item.version} value={item.version}>
                  v{String(item.version).padStart(3, "0")}
                </option>
              ))}
            </select>
          </label>
          <div className="compare-grid">
            <pre className="compare-pane">{compareTextA}</pre>
            <pre className="compare-pane">{compareTextB}</pre>
          </div>
        </div>
      )}
    </Modal>
  );
}
