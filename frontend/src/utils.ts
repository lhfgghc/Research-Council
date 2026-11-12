export function formatTokens(value?: number | null): string {
  if (!value) return "0";
  if (value >= 1000) {
    const thousands = value / 1000;
    return `${thousands >= 10 ? Math.round(thousands) : thousands.toFixed(1)}k`;
  }
  return String(value);
}

export function formatDate(value?: string | null): string {  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function statusLabel(status?: string): string {
  const labels: Record<string, string> = {
    DRAFT: "草稿",
    RUNNING: "讨论中",
    PAUSED: "已暂停",
    WAITING_USER: "等待输入",
    COMPLETED: "已完成",
    COMPLETED_WITH_WARNINGS: "已完成",
    BUDGET_EXHAUSTED: "预算耗尽",
    FAILED: "运行失败",
    CANCELLED: "已终止",
    NEEDS_LITERATURE: "已完成",
    READY_FOR_EXPERIMENT: "可进入实验",
    INCOMPLETE: "未完成",
    ARCHIVED: "已归档",
  };
  return status ? labels[status] || status : "未开始";
}

export function statusTone(status?: string): string {
  if (!status) return "neutral";
  if (["RUNNING", "READY_FOR_EXPERIMENT"].includes(status)) return "positive";
  if (["COMPLETED", "COMPLETED_WITH_WARNINGS", "NEEDS_LITERATURE"].includes(status)) return "complete";
  if (["FAILED", "CANCELLED"].includes(status)) return "danger";
  if (["PAUSED", "BUDGET_EXHAUSTED", "INCOMPLETE", "WAITING_USER"].includes(status)) return "warning";
  return "neutral";
}

export function roleInitial(role: string): string {
  const map: Record<string, string> = {
    pi: "PI",
    domain_researcher_1: "A",
    domain_researcher_2: "B",
    domain_researcher_3: "C",
    method_architect: "M",
    experiment_designer: "E",
    literature_researcher: "文",
    writer: "W",
    coherence_editor: "C",
    user: "你",
  };
  return map[role] || role.slice(0, 1).toUpperCase();
}

export function severityLabel(severity: string): string {
  const map: Record<string, string> = {
    blocking: "阻断",
    major: "重要",
    minor: "次要",
  };
  return map[severity] || severity || "重要";
}

export function phaseLabel(phase: string): string {
  const labels: Record<string, string> = {
    P0: "项目定义",
    P1: "独立构思",
    P1L: "文献调研",
    P2: "团队讨论",
    P3: "Idea 深化",
    P4: "组合成稿",
    DONE: "完成",
    R0: "反馈分析",
    R1: "方法修订",
    R2: "实验修订",
    R3: "修订 · 摘要引言",
    R3A: "修订 · 相关工作与方法",
    R3B: "修订 · 实验与讨论",
    R4: "一致性检查",
    R5: "修复 · 摘要引言",
    R5A: "修复 · 相关工作与方法",
    R5B: "修复 · 实验与讨论",
    R6: "保存版本",
  };
  return labels[phase] || phase;
}

export function truncate(value: string, length = 180): string {
  if (value.length <= length) return value;
  return `${value.slice(0, length).trim()}…`;
}
