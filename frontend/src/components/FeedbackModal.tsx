import { useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api";
import type { Idea } from "../types";
import { Icon } from "./Icons";
import { Modal } from "./UI";

export function FeedbackModal({
  idea,
  onClose,
  onCreated,
}: {
  idea: Idea;
  onClose: () => void;
  onCreated: (runId: string) => void;
}) {
  const [instructions, setInstructions] = useState("");
  const [reportText, setReportText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [maxCalls, setMaxCalls] = useState(20);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!reportText.trim() && !file && !instructions.trim()) {
      setError("请填写修订要求、补充实验观察，或上传报告。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const form = new FormData();
      form.append("instructions", instructions);
      form.append("report_text", reportText);
      form.append("max_calls", String(maxCalls));
      form.append("base_version", String(idea.current_version));
      if (file) form.append("file", file);
      const result = await api.submitFeedback(idea.id, form);
      onCreated(result.run.id);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "提交失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title="继续讨论与修订 Idea"
      subtitle={`${idea.title} · 基础版本 v${String(idea.current_version).padStart(3, "0")}`}
      onClose={onClose}
      wide
    >
      <form className="form-stack" onSubmit={submit}>
        <div className="evidence-note">
          <Icon name="beaker" size={18} />
          <p>
            可以补充实验结果，也可以直接提出想调整的方法或实验。修订将生成新版本和修订说明，原版本可随时回看。
          </p>
        </div>
        <label className="field">
          <span>补充实验结果或观察</span>
          <textarea
            rows={8}
            value={reportText}
            onChange={(event) => setReportText(event.target.value)}
            placeholder="例如：同预算对照下，方法只在任务族 A 上有提升，任务族 B 没有变化。"
          />
        </label>
        <label className="file-drop">
          <Icon name="upload" size={18} />
          <span>{file ? file.name : "可选：上传 PDF / Markdown / TXT / JSON / CSV 报告"}</span>
          <input
            type="file"
            accept=".pdf,.md,.txt,.json,.csv,.yaml,.yml"
            onChange={(event) => setFile(event.target.files?.[0] || null)}
          />
        </label>
        <label className="field">
          <span>修订要求</span>
          <textarea
            rows={3}
            value={instructions}
            onChange={(event) => setInstructions(event.target.value)}
            placeholder="例如：保留原研究问题，结合这次结果调整方法，并设计下一轮对照。"
          />
        </label>
        <label className="field compact">
          <span>本次调用预算</span>
          <input
            type="number"
            min={6}
            max={80}
            value={maxCalls}
            onChange={(event) => setMaxCalls(Number(event.target.value))}
          />
        </label>
        {error && <p className="form-error">{error}</p>}
        <div className="modal-actions">
          <button type="button" className="button ghost" onClick={onClose}>取消</button>
          <button className="button primary" disabled={busy}>
            {busy ? "正在创建修订讨论…" : "启动修订讨论"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
