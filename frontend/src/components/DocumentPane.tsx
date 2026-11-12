import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { api } from "../api";
import type { Idea, RunDetail } from "../types";
import { statusLabel, statusTone } from "../utils";
import { Icon } from "./Icons";
import { Badge, EmptyState } from "./UI";

function headingSlug(text: string, used: Map<string, number>): string {
  const base =
    text
      .trim()
      .toLowerCase()
      .replace(/[^\p{L}\p{N}\s-]/gu, "")
      .replace(/\s+/g, "-")
      .slice(0, 60) || "section";
  const count = used.get(base) || 0;
  used.set(base, count + 1);
  return count === 0 ? base : `${base}-${count}`;
}

export function MarkdownView({ content }: { content: string }) {
  const used = useMemo(() => new Map<string, number>(), [content]);
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={{
          a: (props) => <a {...props} target="_blank" rel="noreferrer" />,
          h1: ({ children }) => {
            const text = flattenChildren(children);
            return <h1 id={headingSlug(text, used)}>{children}</h1>;
          },
          h2: ({ children }) => {
            const text = flattenChildren(children);
            return <h2 id={headingSlug(text, used)}>{children}</h2>;
          },
          h3: ({ children }) => {
            const text = flattenChildren(children);
            return <h3 id={headingSlug(text, used)}>{children}</h3>;
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

function flattenChildren(children: ReactNode): string {
  if (children === null || children === undefined) return "";
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(flattenChildren).join("");
  if (typeof children === "object" && "props" in (children as never)) {
    return flattenChildren((children as { props: { children?: ReactNode } }).props.children);
  }
  return "";
}

export function extractHeadings(content: string): { level: number; text: string; slug: string }[] {
  const used = new Map<string, number>();
  const headings: { level: number; text: string; slug: string }[] = [];
  for (const match of content.matchAll(/^(#{1,3})\s+(.+)$/gm)) {
    const text = match[2].replace(/[$*`]/g, "").trim();
    if (!text) continue;
    headings.push({ level: match[1].length, text, slug: headingSlug(text, used) });
  }
  return headings;
}

export function DocumentPane({
  run,
  refreshKey,
  selectedIdeaId,
  onSelectedIdeaChange,
}: {
  run: RunDetail;
  refreshKey: number;
  selectedIdeaId: string | null;
  onSelectedIdeaChange: (ideaId: string) => void;
}) {
  const [idea, setIdea] = useState<Idea | null>(null);
  const [version, setVersion] = useState<number | null>(null);
  const [displayDocument, setDisplayDocument] = useState("");
  const [displayRevision, setDisplayRevision] = useState("");
  const [mode, setMode] = useState<"document" | "revision">("document");
  const [loading, setLoading] = useState(false);
  const loadedIdeaRef = useRef<string | null>(null);
  const documentScrollRef = useRef<HTMLDivElement | null>(null);
  const [tocOpen, setTocOpen] = useState(false);

  const activeContent = mode === "revision" ? displayRevision : displayDocument;
  const headings = useMemo(() => extractHeadings(activeContent || ""), [activeContent]);

  function jumpToHeading(slug: string) {
    const container = documentScrollRef.current;
    if (!container) return;
    const target = container.querySelector(`#${CSS.escape(slug)}`);
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      setTocOpen(false);
    }
  }

  const selectedIdea = useMemo(
    () => run.ideas.find((item) => item.id === selectedIdeaId) || run.ideas[0] || null,
    [run.ideas, selectedIdeaId],
  );

  useEffect(() => {
    if (selectedIdea && selectedIdea.id !== selectedIdeaId) {
      onSelectedIdeaChange(selectedIdea.id);
    }
  }, [selectedIdea, selectedIdeaId, onSelectedIdeaChange]);

  useEffect(() => {
    if (!selectedIdea) {
      setIdea(null);
      loadedIdeaRef.current = null;
      return;
    }
    let cancelled = false;
    if (loadedIdeaRef.current !== selectedIdea.id) {
      setLoading(true);
      loadedIdeaRef.current = selectedIdea.id;
    }
    api
      .getIdea(selectedIdea.id)
      .then((payload) => {
        if (cancelled) return;
        setIdea(payload);
        setVersion(payload.current_version || null);
        setDisplayDocument(payload.document || "");
        setDisplayRevision(payload.revision_document || "");
      })
      .catch(() => {
        if (!cancelled) setIdea(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedIdea?.id, refreshKey]);

  useEffect(() => {
    if (!idea || !version || version === idea.current_version) return;
    let cancelled = false;
    setLoading(true);
    api
      .getIdeaVersion(idea.id, version)
      .then((payload) => {
        if (cancelled) return;
        setDisplayDocument(payload.document);
        setDisplayRevision(payload.revision_document);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [version, idea?.id, idea?.current_version]);

  if (!selectedIdea) {
    return (
      <div className="document-pane">
        <div className="document-pane-head">
          <div>
            <span className="eyebrow">项目文档</span>
            <h3>等待 Idea 形成</h3>
          </div>
        </div>
        <EmptyState
          title="方案尚未选出"
          description="团队完成候选讨论后，入选 Idea 会在这里以完整 Markdown 文段出现。"
        />
        {run.portfolio_content && (
          <div className="portfolio-preview">
            <span className="eyebrow">组合总览</span>
            <MarkdownView content={run.portfolio_content} />
          </div>
        )}
      </div>
    );
  }

  const versionMeta = idea?.versions.find((item) => item.version === version);
  return (
    <div className="document-pane">
      <div className="document-pane-head">
        <div>
          <span className="eyebrow">当前 Idea · v{String(version || selectedIdea.current_version).padStart(3, "0")}</span>
          <h3>{selectedIdea.title}</h3>
        </div>
        <select
          className="compact-select"
          value={selectedIdea.id}
          onChange={(event) => onSelectedIdeaChange(event.target.value)}
          aria-label="选择 Idea"
        >
          {run.ideas.map((item) => (
            <option key={item.id} value={item.id}>
              {item.title}
            </option>
          ))}
        </select>
      </div>

      <div className="document-toolbar">
        <div className="segmented small">
          <button className={mode === "document" ? "active" : ""} onClick={() => setMode("document")}>
            方案正文
          </button>
          <button
            className={mode === "revision" ? "active" : ""}
            onClick={() => setMode("revision")}
            disabled={!versionMeta?.revision_path}
          >
            修订说明
          </button>
        </div>
        <div className="document-meta">
          <Badge tone={statusTone(idea?.status)}>{statusLabel(idea?.status)}</Badge>
          {idea && idea.versions.length > 1 && (
            <select
              className="compact-select"
              value={version || ""}
              onChange={(event) => setVersion(Number(event.target.value))}
              aria-label="选择版本"
            >
              {idea.versions.map((item) => (
                <option key={item.version} value={item.version}>
                  v{String(item.version).padStart(3, "0")}
                </option>
              ))}
            </select>
          )}
        </div>
      </div>

      {headings.length >= 3 && (
        <div className={`doc-toc ${tocOpen ? "open" : ""}`}>
          <button className="doc-toc-toggle" onClick={() => setTocOpen((open) => !open)}>
            <Icon name="book" size={13} /> 目录（{headings.length} 节）
            <span className="doc-toc-arrow">{tocOpen ? "▾" : "▸"}</span>
          </button>
          {tocOpen && (
            <nav className="doc-toc-list">
              {headings.map((heading, index) => (
                <button
                  key={`${heading.slug}-${index}`}
                  className={`doc-toc-item level-${heading.level}`}
                  onClick={() => jumpToHeading(heading.slug)}
                  title={heading.text}
                >
                  {heading.text}
                </button>
              ))}
            </nav>
          )}
        </div>
      )}

      <div className="document-scroll" ref={documentScrollRef}>
        {loading ? (
          <div className="skeleton-lines" aria-label="正在加载文档">
            <span />
            <span />
            <span />
            <span />
          </div>
        ) : (
          <MarkdownView
            content={
              mode === "revision"
                ? displayRevision || "当前版本没有单独的修订说明。"
                : displayDocument || "文档正在生成。"
            }
          />
        )}
      </div>

      {run.portfolio_content && (
        <details className="portfolio-details">
          <summary>
            <Icon name="book" size={15} /> 查看项目组合总览
          </summary>
          <MarkdownView content={run.portfolio_content} />
        </details>
      )}

    </div>
  );
}
