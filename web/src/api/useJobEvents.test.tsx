import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { JobEvent, JobStatus } from "./types";
import { useJobEvents } from "./useJobEvents";

class TestEventSource extends EventTarget {
  static instances: TestEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    super();
    TestEventSource.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  emitStatus(eventId: number, status: JobStatus): void {
    const event: JobEvent = {
      format_version: "1.0.0",
      event_id: eventId,
      job_id: "j_1",
      type: "job",
      payload: { status },
      created_at: "2026-10-08T00:00:00Z",
    };
    this.dispatchEvent(new MessageEvent("job", {
      data: JSON.stringify(event),
      lastEventId: String(eventId),
    }));
  }

  end(): void {
    this.dispatchEvent(new MessageEvent("stream_end", { data: "{}" }));
  }
}

function latestSource(): TestEventSource {
  const source = TestEventSource.instances.at(-1);
  if (source === undefined) throw new Error("事件订阅未建立");
  return source;
}

beforeEach(() => {
  TestEventSource.instances = [];
  vi.stubGlobal("EventSource", TestEventSource);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("作业事件补推", () => {
  it("保留重试前后的全部历史，收到流结束信号后关闭", () => {
    const { result } = renderHook(() => useJobEvents("j_1"));
    const source = latestSource();
    act(() => {
      source.onopen?.();
      source.emitStatus(1, "failed");
      source.emitStatus(2, "running");
      source.emitStatus(3, "succeeded");
    });
    expect(source.closed).toBe(false);
    expect(result.current.events.map((event) => event.payload["status"])).toEqual([
      "failed", "running", "succeeded",
    ]);
    act(() => source.end());
    expect(source.closed).toBe(true);
    expect(result.current).toMatchObject({ connected: false, settled: true, lastEventId: 3 });
    expect(result.current.events).toHaveLength(3);
  });

  it("瞬时断线保留事件并允许 EventSource 自动重连", () => {
    const { result } = renderHook(() => useJobEvents("j_1"));
    const source = latestSource();
    act(() => {
      source.onopen?.();
      source.emitStatus(1, "running");
      source.onerror?.();
    });
    expect(source.closed).toBe(false);
    expect(result.current.connected).toBe(false);
    expect(result.current.events).toHaveLength(1);
    act(() => source.onopen?.());
    expect(result.current.connected).toBe(true);
  });

  it("无事件的旧作业也能正常结束，避免无限重连", () => {
    const { result } = renderHook(() => useJobEvents("j_1"));
    const source = latestSource();
    act(() => source.end());
    expect(source.closed).toBe(true);
    expect(result.current.settled).toBe(true);
    expect(result.current.events).toEqual([]);
  });

  it("重试同一作业时重新连接并补推新一轮记录", () => {
    const { result, rerender } = renderHook(
      ({ revision }) => useJobEvents("j_1", 500, revision),
      { initialProps: { revision: 0 } },
    );
    const first = latestSource();
    act(() => {
      first.emitStatus(1, "failed");
      first.end();
    });
    rerender({ revision: 1 });
    const second = latestSource();
    expect(second).not.toBe(first);
    act(() => {
      second.emitStatus(1, "failed");
      second.emitStatus(2, "running");
      first.end();
    });
    expect(second.closed).toBe(false);
    expect(result.current.settled).toBe(false);
    expect(result.current.events).toHaveLength(2);
  });

  it("切换作业时关闭原连接并清除原作业的展示", () => {
    const { result, rerender } = renderHook(
      ({ jobId }: { jobId: string | null }) => useJobEvents(jobId),
      { initialProps: { jobId: "j_1" as string | null } },
    );
    const source = latestSource();
    act(() => source.emitStatus(1, "running"));
    rerender({ jobId: "j_2" });
    expect(source.closed).toBe(true);
    expect(latestSource().url).toContain("j_2");
    expect(result.current.events).toEqual([]);
    rerender({ jobId: null });
    expect(latestSource().closed).toBe(true);
  });
});
