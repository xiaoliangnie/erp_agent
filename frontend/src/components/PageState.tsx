interface LoadingProps {
  label?: string;
}

export function Loading({ label = "正在读取实时采购数据…" }: LoadingProps) {
  return (
    <div className="page-state" role="status" aria-live="polite">
      <h2>{label}</h2>
      <p>数据来自本地实时镜像库，首次读取会稍慢。</p>
    </div>
  );
}

interface LoadFailedProps {
  title?: string;
  message: string;
  onRetry?: () => void;
}

/**
 * 读不到数据就说清楚是哪一层出的问题，不放占位数字。
 * 离线快照回退已经取消，页面必须经 server.py 访问。
 */
export function LoadFailed({ title = "读不到采购数据", message, onRetry }: LoadFailedProps) {
  return (
    <div className="page-state" role="alert">
      <h2>{title}</h2>
      <p>确认 server.py 正在运行，且 hanli.env 指向的实时镜像库可连通。</p>
      <pre>{message}</pre>
      {onRetry ? (
        <div style={{ marginTop: 16 }}>
          <button type="button" className="btn" onClick={onRetry}>
            重新读取
          </button>
        </div>
      ) : null}
    </div>
  );
}
