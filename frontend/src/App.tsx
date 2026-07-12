import { useEffect, useState } from "react";

type Health = {
  status: string;
  service: string;
  version: string;
};

export function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/health/live")
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<Health>;
      })
      .then(setHealth)
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : "无法连接后端");
      });
  }, []);

  return (
    <main className="shell">
      <p className="eyebrow">PHASE ONE</p>
      <h1>Loop Engineering</h1>
      <p className="summary">
        当前是可运行骨架。下一阶段会在这里展示工作规格、执行过程、验证证据和人工审批。
      </p>
      <section className="status-card" aria-live="polite">
        <span className={health ? "dot ready" : "dot"} />
        <div>
          <strong>{health ? "后端已连接" : error ? "后端未连接" : "正在检查后端"}</strong>
          <p>{health ? `${health.service} · v${health.version}` : error ?? "请稍候"}</p>
        </div>
      </section>
    </main>
  );
}
