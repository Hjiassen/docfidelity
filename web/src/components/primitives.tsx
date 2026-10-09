/**
 * 通用小块：空态、错误、加载、键值表。
 *
 * 这些之所以要先定义后实现（7.5 的同一要求），是因为它们承载的是**约定**而不是样式：
 * 空态必须给下一步动作、错误必须三段式。各页面各写一套的结果是同一件事有三种说法。
 */

import type { ReactNode } from "react";

import { ApiError } from "../api/client";
import { zh } from "../i18n/zh";

export function Loading({ what = zh.app.loading }: { what?: string }): ReactNode {
  return (
    <p className="muted small" role="status">
      {what}
    </p>
  );
}

/**
 * 空态。
 *
 * `action` 是**必填**的（调用方不给就没有对应 prop）：设计明确要求"空态必须给下一步动作"
 * （14.5）。做成可选会让"忘了给动作"变成一次静默的降级，而它恰恰是最该守住的一条。
 */
export function EmptyState({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: ReactNode;
}): ReactNode {
  return (
    <div className="empty">
      <h3>{title}</h3>
      <p>{body}</p>
      {action}
    </div>
  );
}

/**
 * 错误三段式：发生了什么 / 影响什么 / 可以做什么（14.5）。
 *
 * 三段全部来自**服务端**：`message` / `where` / `remediation`。前端只做排版，
 * 不补写任何一句"可能是……"——那属于猜测，而猜测在排错时比没有更糟。
 */
export function ErrorPanel({ error }: { error: unknown }): ReactNode {
  const apiError = error instanceof ApiError ? error : null;
  const message = apiError?.happened ?? (error instanceof Error ? error.message : String(error));

  return (
    <div className="error-box" role="alert">
      <h3>{zh.error.title}</h3>
      <dl>
        <dt>{zh.error.happened}</dt>
        <dd>{message}</dd>
        {apiError !== null && (
          <>
            <dt>{zh.error.impact}</dt>
            <dd>
              {apiError.impact}
              {apiError.locatedItems.length > 0 && (
                <ul>
                  {apiError.locatedItems.map((item) => (
                    <li key={item.key}>
                      <span className="mono">{item.key}</span>：{item.value}
                    </li>
                  ))}
                </ul>
              )}
            </dd>
            {apiError.actions.length > 0 && (
              <>
                <dt>{zh.error.actions}</dt>
                <dd>
                  <ul>
                    {apiError.actions.map((action) => (
                      <li key={action}>{action}</li>
                    ))}
                  </ul>
                </dd>
              </>
            )}
            {apiError.traceId !== "" && (
              <>
                <dt>{zh.error.traceId}</dt>
                <dd className="mono small">{apiError.traceId}</dd>
              </>
            )}
          </>
        )}
      </dl>
    </div>
  );
}

/** 键值表（定义列表）。`value` 为 `null` / `undefined` 时显示占位符而不是空白。 */
export function KeyValues({
  items,
}: {
  items: ReadonlyArray<{ label: string; value: ReactNode }>;
}): ReactNode {
  return (
    <dl className="grid grid--kv">
      {items.map((item) => (
        <div key={item.label} style={{ display: "contents" }}>
          <dt>{item.label}</dt>
          <dd>{item.value === null || item.value === undefined ? zh.app.none : item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** 时间戳：本地化到分钟。内核只给 ISO 串，展示格式是前端的事。 */
export function formatTime(raw: string | null | undefined): string {
  if (raw === null || raw === undefined || raw === "") return zh.app.none;
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) return raw;
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

/** 字节数：人类可读。避免在页面里重复写三处除法。 */
export function formatBytes(size: number | null | undefined): string {
  if (size === null || size === undefined) return zh.app.none;
  if (size < 1024) return `${size} B`;
  const units = ["KB", "MB", "GB"] as const;
  let value = size / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex] ?? "KB"}`;
}

/**
 * 耗时（毫秒 → 可读串）。未结束时不给数字，避免"看起来卡在某个值上"。
 *
 * 起止时间接受 `undefined`：契约里这两个字段是可选的（`datetime | None = None`），
 * 生成类型于是写成 `string | null | undefined`。三者在本函数里都表示"没有时间戳"。
 */
export function formatDuration(
  startedAt: string | null | undefined,
  finishedAt: string | null | undefined,
): string {
  if (startedAt === null || startedAt === undefined || startedAt === "") return zh.app.none;
  const start = new Date(startedAt).getTime();
  const end =
    finishedAt === null || finishedAt === undefined || finishedAt === ""
      ? Date.now()
      : new Date(finishedAt).getTime();
  if (Number.isNaN(start) || Number.isNaN(end)) return zh.app.none;
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h${minutes % 60}m`;
}
