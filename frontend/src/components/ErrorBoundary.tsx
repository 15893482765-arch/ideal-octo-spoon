/** 通用错误边界（捕获子树错误并显示降级 UI）。 */
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  fallback?: (error: Error, reset: () => void) => ReactNode;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // 业务卡按需接入 Sentry / 上报中心；T06 仅做 console 留痕
    // eslint-disable-next-line no-console
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  private reset = () => this.setState({ error: null });

  override render() {
    const { error } = this.state;
    if (error) {
      if (this.props.fallback) return this.props.fallback(error, this.reset);
      return (
        <div role="alert" className="m-4 rounded-md border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
          <p className="font-semibold">界面渲染异常</p>
          <p className="mt-1 text-rose-600">{error.message}</p>
          <button
            type="button"
            onClick={this.reset}
            className="mt-2 rounded-md border border-rose-300 bg-white px-2 py-1 text-xs text-rose-700"
          >
            重试
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
