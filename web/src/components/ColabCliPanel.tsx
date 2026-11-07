import { useEffect, useState } from 'react';
import { apiFetch, apiMessage } from '../lib/gpu-api';

type Usage = {
  available?: boolean;
  reason?: string;
  balance?: number;
  rate_hourly?: number;
  assignments?: number;
  source?: string;
  auth_blocker?: string;
};
type CliStatus = { mode: string; fake: boolean; label: string; real_allowed: boolean; notice: string };
type CliTask = {
  id: string;
  state: string;
  accelerator: string;
  session_name: string;
  exit_code: number | null;
  log: string;
  usage: Usage;
  residual: boolean;
  residual_reason: string;
  auth_blocker: string;
  fake: boolean;
};

const accelerators = ['cpu', 'T4', 'L4', 'G4', 'H100', 'A100'] as const;

export function ColabCliPanel() {
  const [status, setStatus] = useState<CliStatus | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [tasks, setTasks] = useState<CliTask[]>([]);
  const [script, setScript] = useState('print("hello from colab")\n');
  const [accelerator, setAccelerator] = useState<(typeof accelerators)[number]>('cpu');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function refresh() {
    const [nextStatus, nextUsage, nextTasks] = await Promise.all([
      apiFetch<CliStatus>('/api/gpu/colab-cli/status'),
      apiFetch<Usage>('/api/gpu/colab-cli/usage'),
      apiFetch<CliTask[]>('/api/gpu/colab-cli/tasks'),
    ]);
    setStatus(nextStatus);
    setUsage(nextUsage);
    setTasks(nextTasks);
  }

  useEffect(() => {
    let alive = true;
    refresh().catch((cause) => alive && setError(apiMessage(cause)));
    const timer = window.setInterval(() => {
      refresh().catch((cause) => alive && setError(apiMessage(cause)));
    }, 2000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  async function submitTask() {
    setBusy(true);
    setError('');
    try {
      await apiFetch('/api/gpu/colab-cli/tasks', {
        method: 'POST',
        body: JSON.stringify({ script, accelerator, idle_seconds: 30 }),
      });
      await refresh();
    } catch (cause) {
      setError(apiMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  async function release(taskId: string) {
    setError('');
    try {
      await apiFetch(`/api/gpu/colab-cli/tasks/${encodeURIComponent(taskId)}/release`, { method: 'POST', body: '{}' });
      await refresh();
    } catch (cause) {
      setError(apiMessage(cause));
    }
  }

  return (
    <section className="stack">
      <h2>Colab CLI 脚本</h2>
      <p className="muted">{status?.notice || '正在读取适配器状态…'}</p>
      {status?.fake && <p className="status" role="status">当前是模拟模式，日志和用量都来自替身程序，不是真实 CU。</p>}
      <dl className="kv">
        <dt>适配器</dt><dd>{status ? status.label : '…'}</dd>
        <dt>CU 余额</dt>
        <dd>{usage?.available ? `${usage.balance}（${usage.source}）` : '暂不可用'}</dd>
        <dt>观测消耗速率</dt>
        <dd>{usage?.available ? `${usage.rate_hourly}/小时` : '暂不可用或延迟'}</dd>
        <dt>活跃分配</dt>
        <dd>{usage?.available ? usage.assignments : '暂不可用'}</dd>
      </dl>
      {usage?.auth_blocker && <pre className="status error">{usage.auth_blocker}</pre>}
      <label className="field">
        要在 Colab 上运行的 Python
        <textarea rows={8} value={script} onChange={(event) => setScript(event.target.value)} spellCheck={false} />
      </label>
      <label className="field">
        运行时
        <select value={accelerator} onChange={(event) => setAccelerator(event.target.value as (typeof accelerators)[number])}>
          {accelerators.map((item) => (
            <option key={item} value={item} disabled={item !== 'cpu' && !status?.real_allowed}>
              {item === 'cpu' ? 'CPU（不申请 GPU）' : item}
            </option>
          ))}
        </select>
      </label>
      {error && <p className="status error" role="alert">{error}</p>}
      <button className="button" type="button" disabled={busy || status?.mode === 'off' || status?.mode === 'blocked'} onClick={() => void submitTask()}>
        {busy ? '提交中…' : '提交到 Colab'}
      </button>
      {tasks.map((task) => (
        <article key={task.id} className="card">
          <h3>{task.state} · {task.accelerator}{task.fake ? ' · 模拟' : ''}</h3>
          <p className="muted">会话 {task.session_name}{task.exit_code !== null ? ` · 退出 ${task.exit_code}` : ''}</p>
          {task.auth_blocker && <pre className="status error">{task.auth_blocker}</pre>}
          {task.residual && (
            <p className="status error" role="alert">
              未能证明运行时已释放。{task.residual_reason} 下面的按钮只会重试这个任务自己的会话。
            </p>
          )}
          <pre>{task.log || '还没有日志'}</pre>
          <button className="button secondary" type="button" onClick={() => void release(task.id)}>停止并释放</button>
        </article>
      ))}
    </section>
  );
}
