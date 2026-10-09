/**
 * 实时事件流（P09）。
 *
 * 四条来自 P09 规格的要求：分级（info / warn / error）、可暂停自动滚动、可搜索、
 * 可下载 `events.jsonl`。
 *
 * 关于"可下载"
 * ------------
 * 服务端目前**没有** `events.jsonl` 的下载端点（事件文件在工作区里，取件要走产物
 * 或新开一个端点）。这里先在客户端把**已收到的事件**导成 NDJSON——它不是"完整事件流"
 * （浏览器只有订阅期间收到的那部分），故按钮文案与提示都写清楚这一点。
 * 等端点补上后只需把 `onDownload` 换成直链，展示层不动。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { downloadText } from "../api/client";
import type { JobEvent } from "../api/types";
import { zh } from "../i18n/zh";
import { formatTime } from "./primitives";

type Level = "info" | "warn" | "error";

/** 级别优先取事件自带字段；没有就按 `info` 计——**不猜**。 */
function levelOf(event: JobEvent): Level {
  const raw = event.payload["level"];
  return raw === "warn" || raw === "error" ? raw : "info";
}

/** 事件摘要：取第一个像"给人看的一句话"的字段。 */
function summaryOf(event: JobEvent): string {
  for (const key of ["message", "name", "stage", "rule_id", "status", "artifact_id"]) {
    const value = event.payload[key];
    if (typeof value === "string" && value !== "") return value;
  }
  return JSON.stringify(event.payload);
}

export interface EventStreamProps {
  events: readonly JobEvent[];
  connected: boolean;
  settled: boolean;
  /** 用于导出文件名。 */
  jobId: string;
}

export function EventStream({ events, connected, settled, jobId }: EventStreamProps): ReactNode {
  const [keyword, setKeyword] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const boxRef = useRef<HTMLDivElement | null>(null);

  const visible = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    if (needle === "") return events;
    return events.filter(
      (event) =>
        summaryOf(event).toLowerCase().includes(needle) ||
        event.type.toLowerCase().includes(needle) ||
        zh.event.type[event.type].includes(needle),
    );
  }, [events, keyword]);

  useEffect(() => {
    if (!autoScroll) return;
    const box = boxRef.current;
    if (box === null) return;
    box.scrollTop = box.scrollHeight;
  }, [visible, autoScroll]);

  const download = (): void => {
    const body = events.map((event) => JSON.stringify(event)).join("\n");
    downloadText(`${jobId}-events.jsonl`, `${body}\n`, "application/x-ndjson");
  };

  return (
    <div className="stack">
      <div className="row row--wrap">
        <input
          type="search"
          value={keyword}
          placeholder={zh.p09.eventSearchPlaceholder}
          onChange={(event) => setKeyword(event.target.value)}
          style={{ maxWidth: 240 }}
        />
        <button type="button" className="btn btn--sm" onClick={() => setAutoScroll(!autoScroll)}>
          {autoScroll ? zh.p09.pauseScroll : zh.p09.resumeScroll}
        </button>
        <button type="button" className="btn btn--sm" onClick={download}>
          {zh.p09.downloadEvents}
        </button>
        <span className={connected || settled ? "tag tag--ok" : "tag tag--warn"}>
          {settled ? zh.p09.streamSettled : connected ? zh.p09.streamConnected : zh.p09.streamDisconnected}
        </span>
      </div>

      <div className="events" ref={boxRef} role="log" aria-live="polite">
        {visible.length === 0 ? (
          <p className="faint small">{zh.p09.eventEmpty}</p>
        ) : (
          visible.map((event) => (
            <div key={event.event_id} className={`events__line events__line--${levelOf(event)}`}>
              <span className="events__seq">{event.event_id}</span>
              <span className="events__name">{zh.event.type[event.type]}</span>
              <span className="events__name faint">{formatTime(event.created_at)}</span>
              <span>{summaryOf(event)}</span>
            </div>
          ))
        )}
      </div>

      <p className="faint small">{zh.p09.downloadEventsNote}</p>
    </div>
  );
}
