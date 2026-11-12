import { useMemo, useState } from "react";
import { api } from "../api";
import type { ModelProfile, Settings, Skill } from "../types";
import { Icon } from "./Icons";

const ROLE_DEFS = [
  ["pi", "负责人 PI"],
  ["domain_researcher_1", "独立构思 A"],
  ["domain_researcher_2", "独立构思 B"],
  ["domain_researcher_3", "独立构思 C"],
  ["method_architect", "方法设计"],
  ["experiment_designer", "实验设计"],
  ["literature_researcher", "文献调研"],
  ["writer", "论文成稿"],
  ["coherence_editor", "一致性检查"],
] as const;

const normalizedUrl = (value: string) => value.trim().replace(/\/+$/, "");

export function SettingsView({
  settings,
  skills,
  onUpdated,
  onBack,
}: {
  settings: Settings | null;
  skills: Skill[];
  onUpdated: (settings: Settings) => void;
  onBack: () => void;
}) {
  const [tavilyKey, setTavilyKey] = useState("");
  const [skillsDirs, setSkillsDirs] = useState((settings?.skills_dirs || []).join("\n"));
  const [defaultSkills, setDefaultSkills] = useState<string[]>(settings?.default_skills || []);
  const [writingSkills, setWritingSkills] = useState<string[]>(settings?.writing_skills || []);
  const [maxRunCost, setMaxRunCost] = useState(settings?.max_run_cost_usd ?? 0);
  const [compressionEnabled, setCompressionEnabled] = useState(
    settings?.context_compression_enabled ?? true,
  );
  const [compressionTrigger, setCompressionTrigger] = useState(
    settings?.context_compression_trigger || 0.7,
  );
  const [compressionHard, setCompressionHard] = useState(
    settings?.context_compression_hard || 0.85,
  );
  const [profiles, setProfiles] = useState<ModelProfile[]>(settings?.model_profiles || []);
  const [roleModels, setRoleModels] = useState<Record<string, string[]>>(
    settings?.role_models || {},
  );
  const [discussionModels, setDiscussionModels] = useState<string[]>(
    settings?.discussion_models ?? settings?.model_profiles.map((profile) => profile.id) ?? [],
  );
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState("");
  const [revealing, setRevealing] = useState("");
  const [visibleKeys, setVisibleKeys] = useState<Record<string, string>>({});
  const [modelStatus, setModelStatus] = useState<Record<string, { text: string; ok: boolean }>>({});
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const orderedSkills = useMemo(
    () => [...skills].sort((a, b) => a.id.localeCompare(b.id)),
    [skills],
  );
  const configuredCount = settings?.model_profiles.filter((profile) => profile.base_url && profile.has_api_key).length || 0;

  async function save() {
    setError("");
    setMessage("");
    if (Number(compressionTrigger) >= Number(compressionHard)) {
      setError("压缩触发比例必须小于硬上限。");
      return;
    }
    if (!profiles.length || profiles.some((profile) => !profile.model.trim() || !normalizedUrl(profile.base_url))) {
      setError("请至少保留一个模型，并填写每个模型的 ID 和 Base URL。");
      return;
    }
    const unassigned = ROLE_DEFS.find(([role]) => !roleModels[role]?.length);
    if (!discussionModels.length) {
      setError("请至少选择一个讨论成员。");
      return;
    }
    if (unassigned) {
      setError(`请为「${unassigned[1]}」分配模型。`);
      return;
    }
    setBusy(true);
    try {
      const updated = await api.updateSettings({
        tavily_api_key: tavilyKey.trim(),
        skills_dirs: skillsDirs.split("\n").map((item) => item.trim()).filter(Boolean),
        default_skills: defaultSkills,
        writing_skills: writingSkills,
        max_run_cost_usd: Number(maxRunCost),
        context_compression_enabled: compressionEnabled,
        context_compression_trigger: Number(compressionTrigger),
        context_compression_hard: Number(compressionHard),
        model_profiles: profiles.map(({ has_api_key: _configured, ...profile }) => ({
          ...profile,
          model: profile.model.trim(),
          base_url: normalizedUrl(profile.base_url),
          api_key: profile.api_key?.trim(),
        })),
        role_models: Object.fromEntries(ROLE_DEFS.map(([role]) => [role, roleModels[role].slice(0, 1)])),
        discussion_models: discussionModels,
      });
      setProfiles(updated.model_profiles);
      setRoleModels(updated.role_models);
      setDiscussionModels(updated.discussion_models ?? []);
      setVisibleKeys({});
      setTavilyKey("");
      setModelStatus({});
      onUpdated(updated);
      setMessage("设置已保存。");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function test(profile: ModelProfile) {
    setTesting(profile.id);
    try {
      const result = await api.testModel(profile.id);
      setModelStatus((current) => ({ ...current, [profile.id]: { text: `连接成功 · ${result.model}`, ok: true } }));
    } catch (exc) {
      setModelStatus((current) => ({ ...current, [profile.id]: { text: exc instanceof Error ? exc.message : "连接失败", ok: false } }));
    } finally {
      setTesting("");
    }
  }

  function hideKey(id: string) {
    setVisibleKeys(({ [id]: _hidden, ...otherKeys }) => otherKeys);
  }

  async function toggleKey(profile: ModelProfile, hasSavedKey: boolean) {
    if (Object.hasOwn(visibleKeys, profile.id)) {
      hideKey(profile.id);
      return;
    }
    setRevealing(profile.id);
    try {
      const key = profile.api_key ?? (hasSavedKey ? (await api.revealModelKey(profile.id)).api_key : "");
      setVisibleKeys((current) => ({ ...current, [profile.id]: key }));
    } catch (exc) {
      setModelStatus((current) => ({ ...current, [profile.id]: { text: exc instanceof Error ? exc.message : "读取失败", ok: false } }));
    } finally {
      setRevealing("");
    }
  }

  function toggleDefault(id: string) {
    setDefaultSkills((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );
  }

  function toggleWriting(id: string) {
    setWritingSkills((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );
  }

  function updateProfile(index: number, patch: Partial<ModelProfile>) {
    setProfiles((current) =>
      current.map((profile, itemIndex) =>
        itemIndex === index ? { ...profile, ...patch } : profile,
      ),
    );
  }

  function addProfile() {
    const suffix = Array.from(crypto.getRandomValues(new Uint32Array(2)), (word) => word.toString(16).padStart(8, "0")).join("");
    const id = `model_${suffix}`;
    setDiscussionModels((current) => [...current, id]);
    setProfiles((current) => [
      ...current,
      {
        id,
        name: `模型 ${current.length + 1}`,
        model: "",
        base_url: "",
        api_format: "openai",
        api_key: "",
        has_api_key: false,
        reasoning_effort: "low",
        context_window_tokens: 1000000,
        max_input_tokens: null,
        input_price_per_million: 0.15,
        output_price_per_million: 0.6,
        strengths: [],
      },
    ]);
  }

  function removeProfile(id: string) {
    hideKey(id);
    setDiscussionModels((current) => current.filter((item) => item !== id));
    setProfiles((current) => current.filter((profile) => profile.id !== id));
    setRoleModels((current) => {
      const next: Record<string, string[]> = {};
      for (const [role, ids] of Object.entries(current)) {
        const filtered = ids.filter((item) => item !== id);
        if (filtered.length) next[role] = filtered;
      }
      return next;
    });
  }

  function setSingleRoleModel(role: string, profileId: string) {
    setRoleModels((current) => ({ ...current, [role]: profileId ? [profileId] : [] }));
  }

  return (
    <div className="settings-page">
      <header className="page-header">
        <div>
          <button className="text-button back-link" onClick={onBack}>
            返回工作台
          </button>
          <span className="eyebrow">系统设置</span>
          <h1>模型与科研技能</h1>
        </div>
        <div className="settings-header-actions">
          <div className="settings-status">
            <span className={`status-dot ${configuredCount ? "positive" : "danger"}`} />
            {configuredCount} 个模型已配置
          </div>
          <button className="button primary" onClick={save} disabled={busy || Boolean(revealing || testing)}>
            {busy ? "正在保存…" : "保存设置"}
          </button>
        </div>
      </header>
      {message && <p className="form-success settings-feedback" role="status">{message}</p>}
      {error && <p className="form-error settings-feedback" role="alert">{error}</p>}

      <div className="settings-grid">
        <section className="settings-card settings-card-wide">
          <div className="settings-card-head">
            <div>
              <span className="eyebrow">模型团队</span>
              <h2>讨论成员与任务分工</h2>
            </div>
            <button className="button ghost small" onClick={addProfile}>
              <Icon name="plus" size={14} /> 添加模型
            </button>
          </div>
          <p className="muted">
            讨论成员参与团队讨论和逐 Idea 辩论。其余任务各指定一个模型。
          </p>
          {profiles.length === 0 && (
            <p className="muted">添加模型后，即可为各角色分配成员。</p>
          )}
          <div className="model-profile-list">
            {profiles.map((profile, index) => {
              const saved = settings?.model_profiles.find((item) => item.id === profile.id);
              const sameEndpoint = saved?.base_url === normalizedUrl(profile.base_url);
              const hasSavedKey = Boolean(saved?.has_api_key && sameEndpoint);
              const visible = Object.hasOwn(visibleKeys, profile.id);
              const status = modelStatus[profile.id];
              const apiFormat = profile.api_format ?? "openai";
              const canTest = hasSavedKey && saved?.model === profile.model.trim() &&
                (saved?.api_format ?? "openai") === apiFormat && profile.api_key === undefined;
              return (
              <div className="model-profile-row" key={profile.id}>
                <div className="model-profile-head">
                  <strong>{profile.name || profile.id}</strong>
                  <div className="model-profile-actions">
                    <button className="button ghost small" onClick={() => void test(profile)} disabled={!canTest || Boolean(testing) || busy} title="测试该模型已保存的连接">
                      {testing === profile.id ? "测试中…" : "测试连接"}
                    </button>
                    <button className="text-button" onClick={() => removeProfile(profile.id)} disabled={busy || revealing === profile.id || testing === profile.id}>移除</button>
                  </div>
                </div>
                {status && <p className={status.ok ? "form-success" : "form-error"} role="status">{status.text}</p>}
                <div className="form-grid three">
                  <label className="field compact">
                    <span>显示名称</span>
                    <input
                      value={profile.name}
                      onChange={(event) => updateProfile(index, { name: event.target.value })}
                    />
                  </label>
                  <label className="field compact">
                    <span>API 格式</span>
                    <select value={apiFormat} onChange={(event) => {
                      const format = event.target.value as ModelProfile["api_format"];
                      updateProfile(index, { api_format: format, reasoning_effort: format === "anthropic" ? "off" : "low" });
                    }}>
                      <option value="openai">OpenAI Chat Completions</option>
                      <option value="anthropic">Anthropic Messages</option>
                    </select>
                  </label>
                  <label className="field compact">
                    <span>模型 ID</span>
                    <input
                      value={profile.model}
                      onChange={(event) => updateProfile(index, { model: event.target.value })}
                      placeholder="例如 deepseek/deepseek-v4.1-flash"
                    />
                  </label>
                  <label className="field compact">
                    <span>Base URL</span>
                    <input
                      value={profile.base_url}
                      placeholder="https://provider.example/v1"
                      onChange={(event) => {
                        hideKey(profile.id);
                        updateProfile(index, { base_url: event.target.value });
                      }}
                    />
                  </label>
                  <div className="field compact">
                    <label htmlFor={`key-${profile.id}`}>API Key</label>
                    <div className="model-key-field">
                      <input
                        id={`key-${profile.id}`}
                        type={visible ? "text" : "password"}
                        value={profile.api_key ?? visibleKeys[profile.id] ?? ""}
                        onChange={(event) => updateProfile(index, { api_key: event.target.value })}
                        placeholder={profile.api_key === "" && hasSavedKey ? "保存后清除" : hasSavedKey ? "已保存" : "输入该模型的 Key"}
                        autoComplete="off"
                        spellCheck={false}
                        disabled={busy || revealing === profile.id}
                      />
                      <button className="button ghost small" type="button" disabled={Boolean(revealing) || busy} onClick={() => void toggleKey(profile, hasSavedKey)} aria-label={`${visible ? "隐藏" : "显示"} ${profile.name || profile.model} 的 API Key`}>
                        {revealing === profile.id ? "读取中…" : visible ? "隐藏" : "显示"}
                      </button>
                      {(profile.api_key || (hasSavedKey && profile.api_key === undefined)) && (
                        <button className="text-button" type="button" disabled={busy || revealing === profile.id} onClick={() => {
                          hideKey(profile.id);
                          updateProfile(index, { api_key: "" });
                        }}>清除</button>
                      )}
                    </div>
                    {saved?.has_api_key && !sameEndpoint && profile.api_key === undefined && <small>地址已修改，请填写该地址的 Key。</small>}
                  </div>
                  <label className="field compact">
                    <span>上下文窗口 / token</span>
                    <input
                      type="number"
                      min={8000}
                      step={1000}
                      value={profile.context_window_tokens}
                      onChange={(event) =>
                        updateProfile(index, {
                          context_window_tokens: Number(event.target.value),
                        })
                      }
                    />
                  </label>
                  <label className="field compact">
                    <span>最大输入 / token</span>
                    <input type="number" min={4000} value={profile.max_input_tokens ?? ""} placeholder="留空 = 窗口的 70%" onChange={(event) => updateProfile(index, { max_input_tokens: event.target.value ? Number(event.target.value) : null })} />
                  </label>
                </div>
                <details className="model-profile-options">
                  <summary>推理、价格与能力标签</summary>
                  <div className="form-grid three">
                    <label className="field compact">
                      <span>{apiFormat === "anthropic" ? "自适应思考强度" : "DeepSeek 思考强度"}</span>
                      <select value={profile.reasoning_effort} onChange={(event) => updateProfile(index, { reasoning_effort: event.target.value })}>
                        {["off", "low", "medium", "high", "xhigh", "max"].map((value) => <option key={value}>{value}</option>)}
                      </select>
                    </label>
                    <label className="field compact">
                      <span>输入价格 / 百万 token（美元）</span>
                      <input type="number" min={0} step="any" value={profile.input_price_per_million} onChange={(event) => updateProfile(index, { input_price_per_million: Number(event.target.value) })} />
                    </label>
                    <label className="field compact">
                      <span>输出价格 / 百万 token（美元）</span>
                      <input type="number" min={0} step="any" value={profile.output_price_per_million} onChange={(event) => updateProfile(index, { output_price_per_million: Number(event.target.value) })} />
                    </label>
                  <label className="field compact">
                    <span>能力标签（逗号分隔，可路由）</span>
                    <input
                      value={(profile.strengths || []).join(", ")}
                      onChange={(event) =>
                        updateProfile(index, {
                          strengths: event.target.value
                            .split(",")
                            .map((item) => item.trim())
                            .filter(Boolean),
                        })
                      }
                      placeholder="reasoning, method_design, academic_writing"
                    />
                  </label>
                  </div>
                </details>
              </div>
              );
            })}
          </div>
          {profiles.length > 0 && (
            <>
              <fieldset className="discussion-models">
                <legend>讨论成员（{discussionModels.length}）</legend>
                <div className="model-check-list">
                  {profiles.map((profile) => (
                    <label className="model-check" key={profile.id}>
                      <input type="checkbox" checked={discussionModels.includes(profile.id)} onChange={(event) => {
                        const checked = event.target.checked;
                        setDiscussionModels((current) => checked ? [...current, profile.id] : current.filter((id) => id !== profile.id));
                      }} />
                      <span>{profile.name || profile.model || profile.id}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <div className="role-model-grid">
                {ROLE_DEFS.map(([role, label]) => (
                  <div className="role-model-row" key={role}>
                    <strong>{label}</strong>
                    <select
                      aria-label={label}
                      value={(roleModels[role] || [])[0] || ""}
                      onChange={(event) => setSingleRoleModel(role, event.target.value)}
                    >
                      <option value="" disabled>请选择模型</option>
                      {profiles.map((profile) => (
                        <option key={profile.id} value={profile.id}>
                          {profile.name || profile.id}
                        </option>
                      ))}
                    </select>
                  </div>
                ))}
              </div>
            </>
          )}
        </section>

        <section className="settings-card">
          <div className="settings-card-head">
            <div>
              <h2>运行与检索</h2>
            </div>
            <Icon name="cpu" size={21} />
          </div>
          <label className="field">
            <span>Tavily Web Search Key（可选）</span>
            <input
              type="password"
              value={tavilyKey}
              onChange={(event) => setTavilyKey(event.target.value)}
              placeholder={
                settings?.has_tavily_key
                  ? "已保存，留空则保持不变"
                  : "留空时文献检索的 Web 来源使用 DuckDuckGo"
              }
              autoComplete="off"
            />
            <small>
              文献检索默认并行使用 arXiv + OpenAlex + Web；配置 Tavily Key 后 Web 来源改用
              Tavily，质量更稳定。
            </small>
          </label>
          <label className="field compact">
            <span>单次运行费用上限（美元，0 = 不限制）</span>
            <input
              type="number"
              step="0.5"
              min={0}
              value={maxRunCost}
              onChange={(event) => setMaxRunCost(Number(event.target.value))}
            />
            <small>按每次调用的实际计费累计；超过后运行暂停，可在提高上限后恢复，不会超发请求。</small>
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={compressionEnabled}
              onChange={(event) => setCompressionEnabled(event.target.checked)}
            />
            <span>接近预算时自动压缩历史讨论</span>
          </label>
          <div className="form-grid two">
            <label className="field compact">
              <span>压缩触发比例</span>
              <input
                type="number"
                min={0.3}
                max={0.95}
                step={0.05}
                value={compressionTrigger}
                onChange={(event) => setCompressionTrigger(Number(event.target.value))}
              />
            </label>
            <label className="field compact">
              <span>硬上限比例</span>
              <input
                type="number"
                min={0.4}
                max={0.98}
                step={0.05}
                value={compressionHard}
                onChange={(event) => setCompressionHard(Number(event.target.value))}
              />
            </label>
          </div>
        </section>

        <section className="settings-card">
          <div className="settings-card-head">
            <div>
              <span className="eyebrow">Skills 来源</span>
              <h2>本地科研技能目录</h2>
            </div>
            <Icon name="book" size={21} />
          </div>
          <label className="field">
            <span>目录列表（每行一个）</span>
            <textarea
              rows={5}
              value={skillsDirs}
              onChange={(event) => setSkillsDirs(event.target.value)}
              placeholder="C:\Users\...\skills"
            />
            <small>保存后，在本页选择扫描到的讨论和写作阶段技能。</small>
          </label>
          <div className="skills-count">
            已发现 <strong>{skills.length}</strong> 个技能
          </div>
          <div className="skill-list compact-list">
            {orderedSkills.map((skill) => (
              <div key={skill.id} className="skill-list-row" title={skill.path}>
                <span>
                  <strong>{skill.id}</strong>
                  <small>{skill.description}</small>
                </span>
              </div>
            ))}
            {skills.length === 0 && <p className="muted">当前目录下没有 SKILL.md。</p>}
          </div>
        </section>

        <section className="settings-card settings-card-wide">
          <div className="settings-card-head">
            <div>
              <span className="eyebrow">讨论阶段技能</span>
              <h2>构思、讨论与实验设计（P0–P3 前半）</h2>
            </div>
            <span className="count-pill">{defaultSkills.length}</span>
          </div>
          <div className="skill-grid">
            {orderedSkills.slice(0, 16).map((skill) => (
              <label
                key={skill.id}
                className={`skill-option ${defaultSkills.includes(skill.id) ? "selected" : ""}`}
              >
                <input
                  type="checkbox"
                  checked={defaultSkills.includes(skill.id)}
                  onChange={() => toggleDefault(skill.id)}
                />
                <span>
                  <strong>{skill.id}</strong>
                  <small>{skill.description}</small>
                </span>
              </label>
            ))}
          </div>
          <p className="muted">
            P0 项目定义、P1/P1L 候选与文献、P2 讨论/辩论/决策、P3 方法与实验阶段注入这组技能，用于提高候选质量与批判强度。
          </p>
        </section>

        <section className="settings-card settings-card-wide">
          <div className="settings-card-head">
            <div>
              <span className="eyebrow">写作阶段技能</span>
              <h2>论文成稿、审稿与一致性检查</h2>
            </div>
            <span className="count-pill">{writingSkills.length}</span>
          </div>
          <div className="skill-grid">
            {orderedSkills.slice(0, 16).map((skill) => (
              <label
                key={skill.id}
                className={`skill-option ${writingSkills.includes(skill.id) ? "selected" : ""}`}
              >
                <input
                  type="checkbox"
                  checked={writingSkills.includes(skill.id)}
                  onChange={() => toggleWriting(skill.id)}
                />
                <span>
                  <strong>{skill.id}</strong>
                  <small>{skill.description}</small>
                </span>
              </label>
            ))}
          </div>
          <p className="muted">
            P3 门禁之后的 A/B/C 分段成稿、一致性检查，以及修订（R0–R6）与 P4 组合总览注入这组技能；最终文档按预期论文格式直接产出。
          </p>
        </section>
      </div>
    </div>
  );
}
