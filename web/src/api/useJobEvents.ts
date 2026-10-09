/**
 * 作业事件流（SSE）订阅。
 *
 * 为什么用 `EventSource` 而不是手写 `fetch` + `ReadableStream`
 * ----------------------------------------------------------
 * `Last-Event-ID` 是 SSE 规范的一部分：浏览器在**自动重连**时会带上它，
 * 而这正是 DD-13.4 / 14.3 要的"断线重连不丢事件"。手写流要自己实现重连退避、
 * 自己维护游标、自己解析帧——三样都容易写错，且错了以后表现是"偶尔漏几条"。
 *
 * 由此得到一条**必须**的处理：服务端在作业进入终态后会结束流，而 `EventSource`
 * 见到连接关闭就重连——若我们不主动 `close()`，就会出现"连上→立刻结束→再连上"
 * 的无限循环，服务端日志里刷满同一个作业的请求。
 * 故：收到服务端的 `stream_end` 即主动关闭。历史中的失败事件可能属于重试前，
 * 不能用任意一条终态 `job` 事件决定关闭。
 */

import { useEffect, useRef, useState } from "react";

import { eventsUrl } from "./client";
import { EVENT_TYPES, HEARTBEAT_FRAME, STREAM_END_FRAME, type JobEvent } from "./types";

/** 内存里保留的事件条数上限。事件流是给人看的，不是归档，故不追求全量。 */
export const DEFAULT_EVENT_LIMIT = 500;

export interface JobEventsState {
  /** 收到的作业事件（含补推的历史）。 */
  events: JobEvent[];
  /** 当前连接是否活着（含心跳判定）。 */
  connected: boolean;
  /** 作业是否已进入终态（此后不会再推事件）。 */
  settled: boolean;
  /** 最后收到的事件序号；用于展示"补推到哪了"。 */
  lastEventId: number;
}

const EMPTY: JobEventsState = { events: [], connected: false, settled: false, lastEventId: 0 };

/**
 * 订阅一个作业的事件流。
 *
 * `jobId` 为 `null` 时不连接（页面还没有作业可看）。
 * `revision` 在同一作业重试 / 续跑后递增，重新补推并订阅。
 */
export function useJobEvents(
  jobId: string | null,
  limit: number = DEFAULT_EVENT_LIMIT,
  revision: number = 0,
): JobEventsState {
  const [state, setState] = useState<JobEventsState>(EMPTY);

  // 放到 ref 里而不是闭包捕获：`limit` 变了不该重建连接（重连会白白补推一遍历史）。
  const limitRef = useRef(limit);
  limitRef.current = limit;

  useEffect(() => {
    if (jobId === null) {
      setState(EMPTY);
      return;
    }

    setState(EMPTY);
    const source = new EventSource(eventsUrl(jobId));
    let closed = false;

    const close = (): void => {
      if (closed) return;
      closed = true;
      source.close();
    };

    source.onopen = () => {
      if (closed) return;
      setState((prev) => ({ ...prev, connected: true }));
    };

    source.onerror = () => {
      if (closed) return;
      // 断开只更新展示状态。重连由浏览器负责——包括带上 Last-Event-ID。
      setState((prev) => ({ ...prev, connected: false }));
    };

    source.addEventListener(HEARTBEAT_FRAME, () => {
      if (closed) return;
      // 心跳的唯一作用是证明"流还活着"（长任务里"界面没反应"和"任务卡住"必须可区分）。
      setState((prev) => ({ ...prev, connected: true }));
    });

    source.addEventListener(STREAM_END_FRAME, () => {
      if (closed) return;
      setState((prev) => ({ ...prev, settled: true, connected: false }));
      close();
    });

    for (const type of EVENT_TYPES) {
      source.addEventListener(type, (raw: Event) => {
        if (closed) return;
        const message = raw as MessageEvent<string>;
        let event: JobEvent;
        try {
          event = JSON.parse(message.data) as JobEvent;
        } catch {
          // 帧解析不了就丢掉这一条：不因为一条坏帧把整条流判死。
          return;
        }
        const reported = Number(message.lastEventId);
        setState((prev) => ({
          ...prev,
          events: [...prev.events, event].slice(-limitRef.current),
          lastEventId: Number.isFinite(reported) ? reported : prev.lastEventId,
        }));
      });
    }

    return close;
  }, [jobId, revision]);

  return state;
}
