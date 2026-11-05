"""进程内指标注册表（T29 / PRD §9.4 观测）。

设计取舍：
- 首版用**零第三方依赖**的内存注册表（Counter / Gauge / Histogram），
  输出 Prometheus 文本格式（/metrics）。多实例部署由 Prometheus 分别抓取后聚合，
  无需在本服务内引入 push gateway（附录 A 范围外的分布式指标后端不做）。
- 指标采集是**非关键旁路**：任何记录/序列化异常都被吞掉并降级为一次内部错误计数，
  绝不允许拖垮业务请求（Q172：故障时核心业务可用）。
- 不记录高基数标签（user_id、request_id、原始 path 中的 UUID），避免标签爆炸；
  path 使用路由模板，敏感值不入标签。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Iterable

# 指标桶（秒），覆盖 PRD §9.1 阈值：列表 0.8s、工作台 1s、巡访提交 3s。
DEFAULT_BUCKETS = (
    0.05,
    0.1,
    0.2,
    0.4,
    0.8,
    1.0,
    1.5,
    3.0,
    5.0,
    10.0,
)

_INF = float("inf")


def percentile(sorted_values: list[float], pct: float) -> float:
    """线性插值百分位（与 Prometheus/Python quantile 一致），pct∈[0,100]；空返回 0。

    rank = pct/100 * (n-1)，在相邻两阶之间线性插值。供 Q169 压测统计复用。
    """
    if not sorted_values:
        return 0.0
    ordered = sorted(sorted_values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


class Counter:
    __slots__ = ("name", "help", "_values", "_lock", "_label_names")

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...]) -> None:
        self.name = name
        self.help = help_text
        self._label_names = label_names
        self._values: dict[tuple[str, ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._label_key(labels)
        with self._lock:
            self._values[key] += amount

    def value(self, **labels: str) -> float:
        key = self._label_key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def items(self) -> list[tuple[tuple[str, ...], float]]:
        with self._lock:
            return list(self._values.items())

    def _label_key(self, labels: dict[str, str]) -> tuple[str, ...]:
        return tuple(labels.get(n, "") for n in self._label_names)

    @property
    def label_names(self) -> tuple[str, ...]:
        return self._label_names


class Gauge(Counter):
    def set(self, value: float, **labels: str) -> None:
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = value


class Histogram:
    __slots__ = ("name", "help", "buckets", "_counts", "_sums", "_lock", "_label_names")

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...],
        buckets: Iterable[float] = DEFAULT_BUCKETS,
    ) -> None:
        self.name = name
        self.help = help_text
        self.buckets = tuple(sorted(buckets))
        self._label_names = label_names
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def observe(self, value: float, **labels: str) -> None:
        key = tuple(labels.get(n, "") for n in self._label_names)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * (len(self.buckets) + 1))
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[i] += 1
                    break
            else:
                counts[-1] += 1  # +Inf 桶
            self._sums[key] += value

    def snapshot(self) -> dict[tuple[str, ...], tuple[list[int], float]]:
        with self._lock:
            return {k: (list(v), self._sums[k]) for k, v in self._counts.items()}

    @property
    def label_names(self) -> tuple[str, ...]:
        return self._label_names


class MetricsRegistry:
    """线程安全的进程指标注册表。"""

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> Counter:
        with self._lock:
            c = self._counters.get(name)
            if c is None:
                c = Counter(name, help_text, label_names)
                self._counters[name] = c
            return c

    def gauge(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> Gauge:
        with self._lock:
            g = self._gauges.get(name)
            if g is None:
                g = Gauge(name, help_text, label_names)
                self._gauges[name] = g
            return g

    def histogram(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: Iterable[float] = DEFAULT_BUCKETS,
    ) -> Histogram:
        with self._lock:
            h = self._histograms.get(name)
            if h is None:
                h = Histogram(name, help_text, label_names, buckets)
                self._histograms[name] = h
            return h

    # -------- 安全记录：旁路异常不外抛（Q172）--------

    def safe_inc(self, name: str, amount: float = 1.0, **labels: str) -> None:
        try:
            self.counter(name, "").inc(amount, **labels)
        except Exception:  # noqa: BLE001 - 观测旁路永不影响主链路
            try:
                self._counters.get("app_metrics_errors")
            except Exception:  # noqa: BLE001
                pass

    def render_prometheus(self) -> str:
        """渲染 Prometheus 文本 exposition 格式（0.0.4）。"""
        lines: list[str] = []
        with self._lock:
            counters = list(self._counters.values())
            gauges = list(self._gauges.values())
            histograms = list(self._histograms.values())

        for c in counters:
            lines.append(f"# HELP {c.name} {c.help}")
            lines.append(f"# TYPE {c.name} counter")
            for key, val in c.items():
                lines.append(f"{c.name}{format_labels(c.label_names, key)} {val}")
        for g in gauges:
            lines.append(f"# HELP {g.name} {g.help}")
            lines.append(f"# TYPE {g.name} gauge")
            for key, val in g.items():
                lines.append(f"{g.name}{format_labels(g.label_names, key)} {val}")
        for h in histograms:
            lines.append(f"# HELP {h.name} {h.help}")
            lines.append(f"# TYPE {h.name} histogram")
            for key, (counts, total) in h.snapshot().items():
                cumulative = 0
                for i, bound in enumerate(h.buckets):
                    cumulative += counts[i]
                    le = f"{bound:g}"
                    labels = format_labels(h.label_names, key, extra={"le": le})
                    lines.append(f"{h.name}_bucket{labels} {cumulative}")
                cumulative += counts[-1]
                inf_labels = format_labels(h.label_names, key, extra={"le": "+Inf"})
                lines.append(f"{h.name}_bucket{inf_labels} {cumulative}")
                lines.append(f"{h.name}_sum{format_labels(h.label_names, key)} {total}")
                lines.append(f"{h.name}_count{format_labels(h.label_names, key)} {cumulative}")
        return "\n".join(lines) + "\n"


def format_labels(
    names: tuple[str, ...], values: tuple[str, ...], extra: dict[str, str] | None = None
) -> str:
    pairs = [(n, _escape_label(str(v))) for n, v in zip(names, values, strict=False)]
    if extra:
        pairs.extend((k, _escape_label(v)) for k, v in extra.items())
    if not pairs:
        return ""
    return "{" + ",".join(f'{n}="{v}"' for n, v in pairs) + "}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


# ===== 本应用统一登记的指标名（集中声明，避免散落/重名）=====

METRIC_HTTP_REQUESTS = "app_http_requests_total"  # labels: method, route, status_class
METRIC_HTTP_LATENCY = "app_http_request_duration_seconds"  # labels: method, route
METRIC_AUTH_LOGIN_FAILURE = "app_auth_login_failure_total"  # labels: reason
METRIC_PERMISSION_DENIED = "app_permission_denied_total"  # labels: role, module
METRIC_EXPORT = "app_export_total"  # labels: status, format
METRIC_AI_DEGRADED = "app_ai_degraded_total"  # labels: reason
METRIC_AI_TOOL_FAILURE = "app_ai_tool_failure_total"  # labels: tool, reason
METRIC_WORKER_JOB = "app_worker_job_total"  # labels: job, status
METRIC_RATE_LIMITED = "app_http_rate_limited_total"  # labels: bucket
METRIC_METRICS_ERRORS = "app_metrics_errors_total"


# 进程级单例
registry = MetricsRegistry()

# 启动即声明关键计数器，确保 /metrics 始终可见（即使还没流量）。
for _mname, _mhelp, _mlabels in (
    (METRIC_HTTP_REQUESTS, "HTTP 请求总数", ("method", "route", "status_class")),
    (METRIC_AUTH_LOGIN_FAILURE, "登录失败次数", ("reason",)),
    (METRIC_PERMISSION_DENIED, "权限拒绝次数", ("role", "module")),
    (METRIC_EXPORT, "导出任务次数", ("status", "format")),
    (METRIC_AI_DEGRADED, "AI 降级次数", ("reason",)),
    (METRIC_AI_TOOL_FAILURE, "AI 工具失败次数", ("tool", "reason")),
    (METRIC_WORKER_JOB, "异步任务处理次数", ("job", "status")),
    (METRIC_RATE_LIMITED, "被限流请求次数", ("bucket",)),
    (METRIC_METRICS_ERRORS, "指标记录自身失败次数", ()),
):
    registry.counter(_mname, _mhelp, _mlabels)

registry.histogram(
    METRIC_HTTP_LATENCY,
    "HTTP 请求处理耗时（秒）",
    ("method", "route"),
)
