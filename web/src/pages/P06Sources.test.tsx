import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { sourcesApi } from "../api/endpoints";
import { P06Sources } from "./P06Sources";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("来源登记保留领域绑定与接入器选项", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.spyOn(sourcesApi, "list").mockResolvedValue({ items: [], count: 0, known_types: ["openapi"] });
  const create = vi.spyOn(sourcesApi, "create").mockImplementation(() => new Promise(() => {}));
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/projects/p_test/sources"]}>
        <Routes>
          <Route path="/projects/:projectId/sources" element={<P06Sources />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await screen.findByLabelText("来源标识");
  fireEvent.change(screen.getByLabelText("来源标识"), { target: { value: "bound-source" } });
  const bindings = [{ shape: "operation", kind: "item", key: ["path"] }];
  fireEvent.change(screen.getByLabelText("领域绑定 JSON"), { target: { value: JSON.stringify(bindings) } });
  fireEvent.change(screen.getByLabelText("接入器选项 JSON"), { target: { value: '{"scope_default":"default"}' } });
  fireEvent.click(screen.getByRole("button", { name: "登记来源" }));
  await waitFor(() => expect(create).toHaveBeenCalledWith("p_test", expect.objectContaining({
    config: expect.objectContaining({ bindings, options: { scope_default: "default" } }),
  })));
  client.clear();
});
