import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import type { ProjectConfig } from "../api/types";
import { P07Config } from "./P07Config";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function serveConfig(pointId: string, admissionPolicy?: "require_confidence" | "independent_validation"): {
  saved: ProjectConfig[];
  client: QueryClient;
} {
  let current = {
    project_id: "p_test",
    version: 1,
    format_version: "1.0.0",
    documents: [],
    terminology: { format_version: "1.0.0", entries: [] },
    pagination: { format_version: "1.0.0" },
    rule_sets: [],
    routing: {
      format_version: "1.0.0",
      model_enabled: true,
      decision_points: {
        [pointId]: {
          format_version: "1.0.0",
          fallback: "queue",
          model: "custom-model",
          confidence_threshold: 0.8,
          ...(admissionPolicy === undefined ? {} : { admission_policy: admissionPolicy }),
        },
      },
    },
  };
  const saved: ProjectConfig[] = [];
  vi.stubGlobal("fetch", async (input: string, init: RequestInit): Promise<Response> => {
    if (input.endsWith("/config/versions")) {
      return Response.json([{ version: current.version, created_at: "2026-10-13T00:00:00Z", author: null }]);
    }
    if (input.endsWith("/config") && init.method === "PUT") {
      const submitted = JSON.parse(String(init.body)) as ProjectConfig;
      saved.push(submitted);
      current = { ...current, ...submitted } as typeof current;
      return Response.json({ version: submitted.version });
    }
    if (input.endsWith("/config")) return Response.json(current);
    throw new Error(`Unexpected request: ${input}`);
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/projects/p_test/config"]}>
        <Routes>
          <Route path="/projects/:projectId/config" element={<P07Config />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { saved, client };
}

it("旧配置缺少准入策略时显示真实置信度要求", async () => {
  const { client } = serveConfig("placement.section_slot");
  const policy = await screen.findByRole<HTMLSelectElement>("combobox", {
    name: "准入策略 placement.section_slot",
  });
  expect(policy.value).toBe("require_confidence");
  client.clear();
});

it("切换准入策略后保存完整配置且保留路由其他字段", async () => {
  const { saved, client } = serveConfig("placement.section_slot", "require_confidence");
  const policy = await screen.findByRole<HTMLSelectElement>("combobox", {
    name: "准入策略 placement.section_slot",
  });
  fireEvent.change(policy, { target: { value: "independent_validation" } });
  fireEvent.click(screen.getByRole("button", { name: "保存为新版本" }));
  await waitFor(() => expect(saved).toHaveLength(1));
  expect(saved[0]?.routing?.decision_points?.["placement.section_slot"]).toEqual({
    format_version: "1.0.0",
    admission_policy: "independent_validation",
    fallback: "queue",
    model: "custom-model",
    confidence_threshold: 0.8,
  });
  expect(saved[0]?.project_id).toBe("p_test");
  expect(saved[0]?.documents).toEqual([]);
  await waitFor(() => expect(screen.getByRole("button", { name: "保存为新版本" }).hasAttribute("disabled")).toBe(false));
  fireEvent.change(screen.getByRole("combobox", { name: "准入策略 placement.section_slot" }), {
    target: { value: "require_confidence" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存为新版本" }));
  await waitFor(() => expect(saved).toHaveLength(2));
  expect(saved[1]?.routing?.decision_points?.["placement.section_slot"]?.admission_policy).toBe("require_confidence");
  client.clear();
});

it("准入策略适用范围由服务端校验且错误原样显示", async () => {
  const { client } = serveConfig("classification.domain_kind", "require_confidence");
  const realFetch = fetch;
  let submitted: ProjectConfig | undefined;
  vi.stubGlobal("fetch", async (input: string, init: RequestInit): Promise<Response> => {
    if (input.endsWith("/config") && init.method === "PUT") {
      submitted = JSON.parse(String(init.body)) as ProjectConfig;
      return Response.json({
        code: "E_CONFIG_INVALID",
        message: "独立约束校验仅支持 placement.section_slot",
        where: { decision_point: "classification.domain_kind" },
        remediation: ["请选择真实置信度策略"],
        trace_id: "test-trace",
      }, { status: 422 });
    }
    return realFetch(input, init);
  });
  const policy = await screen.findByRole("combobox", { name: "准入策略 classification.domain_kind" });
  fireEvent.change(policy, { target: { value: "independent_validation" } });
  fireEvent.click(screen.getByRole("button", { name: "保存为新版本" }));
  await screen.findByText("独立约束校验仅支持 placement.section_slot");
  expect(submitted?.routing?.decision_points?.["classification.domain_kind"]?.admission_policy).toBe("independent_validation");
  client.clear();
});
