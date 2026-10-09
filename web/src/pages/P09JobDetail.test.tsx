import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, downloadUrl } from "../api/client";
import { jobsApi } from "../api/endpoints";
import type { Artifact, Job, JobDetail, JobStatus } from "../api/types";
import * as jobEvents from "../api/useJobEvents";
import type { JobEventsState } from "../api/useJobEvents";
import { zh } from "../i18n/zh";
import { P09JobDetail } from "./P09JobDetail";

const FIRST_JOB = "j_first";
const SECOND_JOB = "j_second";
const clients: QueryClient[] = [];
let stream: JobEventsState;

function detail(jobId: string, status: JobStatus = "failed", artifactIds: string[] = []): JobDetail {
  return {
    job: {
      format_version: "1.0.0",
      job_id: jobId,
      project_id: "p_test",
      kind: "document",
      doc_key: jobId,
      doc_type: "demo",
      status,
      inputs_hash: "fixed-input",
      created_at: "2026-10-08T00:00:00Z",
      initiated_by: null,
      parent_job_id: null,
      child_job_ids: [],
      artifact_ids: artifactIds,
      progress: null,
      error: null,
    },
    stages: [],
    children: [],
    resume_plan: ["render"],
    progress: null,
  };
}

function artifact(jobId: string): Artifact {
  return {
    format_version: "1.0.0",
    artifact_id: "a_final",
    job_id: jobId,
    kind: "docx",
    relative_path: "artifacts/final.docx",
    size_bytes: 2048,
    sha256: "fixed-output",
    created_at: "2026-10-08T00:00:01Z",
    expires_at: null,
  };
}

function cachedClient(first: JobDetail = detail(FIRST_JOB)): QueryClient {
  const client = new QueryClient({
    defaultOptions: {
      queries: { staleTime: Infinity, retry: false },
      mutations: { retry: false },
    },
  });
  client.setQueryData(["job", FIRST_JOB], first);
  client.setQueryData(["job", SECOND_JOB], detail(SECOND_JOB));
  client.setQueryData(["artifacts", FIRST_JOB, true], []);
  client.setQueryData(["artifacts", SECOND_JOB, true], []);
  clients.push(client);
  return client;
}

function page(client: QueryClient) {
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/projects/p_test/jobs/${FIRST_JOB}`]}>
        <Link to={`/projects/p_test/jobs/${SECOND_JOB}`}>切换到第二作业</Link>
        <Routes>
          <Route path="/projects/:projectId/jobs/:jobId" element={<P09JobDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((fulfilled, rejected) => {
    resolve = fulfilled;
    reject = rejected;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  stream = { events: [], connected: true, settled: false, lastEventId: 0 };
  vi.spyOn(jobEvents, "useJobEvents").mockImplementation(() => stream);
  vi.spyOn(jobsApi, "detail").mockImplementation(async (jobId) => detail(jobId));
  vi.spyOn(jobsApi, "artifacts").mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  for (const client of clients.splice(0)) client.clear();
  vi.restoreAllMocks();
});

describe("作业详情操作与最终产物", () => {
  it("在操作仍 pending 时进入另一缓存作业，不禁用第二作业的操作", async () => {
    const operation = deferred<Job>();
    vi.spyOn(jobsApi, "retry").mockReturnValue(operation.promise);
    const client = cachedClient();
    render(page(client));

    fireEvent.click(screen.getByRole("button", { name: zh.p09.retryStage }));
    await waitFor(() => {
      expect((screen.getByRole("button", { name: zh.p09.retryStage }) as HTMLButtonElement).disabled).toBe(true);
    });
    fireEvent.click(screen.getByRole("link", { name: "切换到第二作业" }));
    expect(screen.getByRole("heading", { name: new RegExp(SECOND_JOB) })).toBeTruthy();
    const secondBusy = (screen.getByRole("button", { name: zh.p09.retryStage }) as HTMLButtonElement).disabled;

    await act(async () => operation.reject(new Error("第一作业的延迟失败")));
    await waitFor(() => {
      expect((screen.getByRole("button", { name: zh.p09.retryStage }) as HTMLButtonElement).disabled).toBe(false);
    });
    expect(secondBusy).toBe(false);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("已失败的操作错误不会带入另一缓存作业", async () => {
    vi.spyOn(jobsApi, "retry").mockRejectedValue(new ApiError({
      status: 409,
      code: "E_STATE_INVALID",
      message: "第一作业暂时不能重试",
      where: { job_id: FIRST_JOB },
      remediation: ["刷新第一作业后再试"],
    }));
    render(page(cachedClient()));
    fireEvent.click(screen.getByRole("button", { name: zh.p09.retryStage }));
    expect(await screen.findByRole("alert")).toBeTruthy();

    fireEvent.click(screen.getByRole("link", { name: "切换到第二作业" }));
    expect(screen.getByRole("heading", { name: new RegExp(SECOND_JOB) })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect((screen.getByRole("button", { name: zh.p09.retryStage }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("导航后第一作业的延迟成功只刷新第一作业缓存", async () => {
    const operation = deferred<Job>();
    vi.spyOn(jobsApi, "retry").mockReturnValue(operation.promise);
    const client = cachedClient();
    const completed = detail(FIRST_JOB, "succeeded");
    vi.mocked(jobsApi.detail).mockImplementation(async (jobId) => (
      jobId === SECOND_JOB ? detail(SECOND_JOB, "succeeded") : completed
    ));
    render(page(client));

    fireEvent.click(screen.getByRole("button", { name: zh.p09.retryStage }));
    await waitFor(() => {
      expect((screen.getByRole("button", { name: zh.p09.retryStage }) as HTMLButtonElement).disabled).toBe(true);
    });
    fireEvent.click(screen.getByRole("link", { name: "切换到第二作业" }));
    await act(async () => operation.resolve(completed.job));
    await waitFor(() => {
      expect(client.getQueryState(["job", FIRST_JOB])?.isInvalidated).toBe(true);
    });
    expect(client.getQueryData<JobDetail>(["job", SECOND_JOB])?.job.status).toBe("failed");
    expect(client.getQueryState(["job", SECOND_JOB])?.isInvalidated).toBe(false);
    expect((screen.getByRole("button", { name: zh.p09.retryStage }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("快速重试直接从 failed 到 succeeded，也会替换同一终态键下的空产物缓存", async () => {
    const output = artifact(FIRST_JOB);
    const completed = detail(FIRST_JOB, "succeeded", [output.artifact_id]);
    const client = cachedClient();
    vi.spyOn(jobsApi, "retry").mockResolvedValue(completed.job);
    vi.mocked(jobsApi.detail).mockResolvedValue(completed);
    vi.mocked(jobsApi.artifacts).mockResolvedValue([output]);
    render(page(client));

    fireEvent.click(screen.getByRole("button", { name: zh.p09.retryStage }));
    await screen.findByText(zh.job.status.succeeded);
    const download = await screen.findByRole("link", { name: zh.p09.download });
    expect(download.getAttribute("href")).toBe(downloadUrl(output.artifact_id));
    expect(screen.getByText(output.relative_path)).toBeTruthy();
    expect(client.getQueryData(["artifacts", FIRST_JOB, true])).toEqual([output]);
  });

  it("stream_end 后重新读取已登记的最终详情和下载列表", async () => {
    const output = artifact(FIRST_JOB);
    const completed = detail(FIRST_JOB, "succeeded", [output.artifact_id]);
    const client = cachedClient(detail(FIRST_JOB, "succeeded"));
    vi.mocked(jobsApi.detail).mockResolvedValue(completed);
    vi.mocked(jobsApi.artifacts).mockResolvedValue([output]);
    const view = render(page(client));
    expect(screen.queryByRole("link", { name: zh.p09.download })).toBeNull();

    stream = { ...stream, connected: false, settled: true };
    view.rerender(page(client));
    const download = await screen.findByRole("link", { name: zh.p09.download });
    expect(download.getAttribute("href")).toBe(downloadUrl(output.artifact_id));
    await waitFor(() => {
      expect(client.getQueryData<JobDetail>(["job", FIRST_JOB])?.job.artifact_ids).toEqual([output.artifact_id]);
    });
    expect(screen.getByText(zh.p09.streamSettled)).toBeTruthy();
  });
});
