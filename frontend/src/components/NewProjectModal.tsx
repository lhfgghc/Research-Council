import { useState } from "react";
import { api } from "../api";
import type { Project } from "../types";
import { Modal } from "./UI";

export function NewProjectModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (project: Project) => void;
}) {
  const [name, setName] = useState("");
  const [topic, setTopic] = useState("");
  const [venue, setVenue] = useState("ACL");
  const [domain, setDomain] = useState("NLP / LLM");
  const [constraints, setConstraints] = useState(
    "只产出 Idea 方案，不执行实验。优先考虑普通实验室可承担的算力与数据条件。",
  );
  const [language, setLanguage] = useState("zh-CN");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!name.trim() || topic.trim().length < 8) {
      setError("请填写项目名称，并用至少一句话描述研究主题。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const project = await api.createProject({
        name: name.trim(),
        topic: topic.trim(),
        venue: venue.trim(),
        domain: domain.trim(),
        constraints: constraints.trim(),
        language,
      });
      onCreated(project);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "创建项目失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title="建立一个新的研究项目"
      subtitle="先定义问题、目标会议与资源边界。讨论会在启动后由 PI 形成议程。"
      onClose={onClose}
      wide
    >
      <form className="form-stack" onSubmit={submit}>
        <div className="form-grid two">
          <label className="field">
            <span>项目名称</span>
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：长上下文推理的机制研究" />
          </label>
          <label className="field">
            <span>目标会议</span>
            <input value={venue} onChange={(event) => setVenue(event.target.value)} placeholder="ACL / NeurIPS / CVPR" list="venue-list" />
            <datalist id="venue-list">
              <option value="ACL" />
              <option value="NeurIPS" />
              <option value="ICLR" />
              <option value="ICML" />
              <option value="CVPR" />
              <option value="SIGMOD" />
              <option value="SOSP" />
            </datalist>
          </label>
        </div>

        <label className="field">
          <span>研究主题</span>
          <textarea
            rows={4}
            value={topic}
            onChange={(event) => setTopic(event.target.value)}
            placeholder="尽量写清楚问题、对象、现有矛盾和你希望产生的认知。"
          />
          <small>系统会把主题转成研究问题，但不会替你预先假定结论。</small>
        </label>

        <div className="form-grid two">
          <label className="field">
            <span>研究方向</span>
            <input value={domain} onChange={(event) => setDomain(event.target.value)} placeholder="NLP / LLM Agents" />
          </label>
          <label className="field">
            <span>正式文档语言</span>
            <select value={language} onChange={(event) => setLanguage(event.target.value)}>
              <option value="zh-CN">中文正文</option>
              <option value="en-US">English manuscript</option>
            </select>
          </label>
        </div>

        <label className="field">
          <span>资源与范围约束</span>
          <textarea
            rows={3}
            value={constraints}
            onChange={(event) => setConstraints(event.target.value)}
            placeholder="算力、数据、时间、不做什么、必须满足的条件。"
          />
        </label>

        {error && <p className="form-error">{error}</p>}
        <div className="modal-actions">
          <button type="button" className="button ghost" onClick={onClose}>
            取消
          </button>
          <button className="button primary" disabled={busy}>
            {busy ? "正在创建…" : "创建项目"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
