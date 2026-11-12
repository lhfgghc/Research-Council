const fields = [
  ["hypothesis", "核心假设"],
  ["inputs", "数据、代码与依赖"],
  ["comparison", "最小对照"],
  ["resource_limit", "资源上限"],
  ["metric", "观察指标"],
  ["continue_if", "继续条件"],
  ["narrow_if", "收窄条件"],
  ["stop_if", "停止条件"],
];

export function ValidationCard({ value }: { value: unknown }) {
  if (!value || typeof value !== "object" || !("status" in value)) return null;
  const card = value as Record<string, unknown>;
  const status = card.status === "ready" ? "可启动" : card.status === "blocked" ? "受阻" : "待确认";
  return (
    <details className="rail-card">
      <summary>首轮验证 · {status}</summary>
      <dl>
        {fields.map(([key, label]) => (
          <div key={key}>
            <dt><strong>{label}</strong></dt>
            <dd>{typeof card[key] === "string" ? card[key] : "待确认"}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
