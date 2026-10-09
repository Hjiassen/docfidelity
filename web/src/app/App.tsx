/**
 * 应用装配：QueryClient + 路由表（DD-14.2）。
 *
 * 路由表严格照需求 7.3.2 的站点地图，**十四页全部已实现**：P01 / P02 / P03 / P04 / P05 /
 * P06 / P07 / P08 / P09 / P10 / P11 / P12 / P13 / P14。其中若干页是**为闭合判据而补入**的：
 * P03 是模板包与档案的落点（缺了它，P05 第 5 步登记完没有地方看得见），P04 是模板包契约的
 * 详情页，P08 是唯一能发起构建的入口，P12 是待裁决徽标的落点；P02 / P07 / P11 / P14 补上
 * 之后，侧边导航里的每一项都有真页面（它们此前是 `Placeholder`——那个组件已随本轮删除）。
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import type { ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { P01Projects } from "../pages/P01Projects";
import { P02Overview } from "../pages/P02Overview";
import { P03Templates } from "../pages/P03Templates";
import { P04TemplateDetail } from "../pages/P04TemplateDetail";
import { P05TemplateImport } from "../pages/P05TemplateImport";
import { P06Sources } from "../pages/P06Sources";
import { P07Config } from "../pages/P07Config";
import { P08Jobs } from "../pages/P08Jobs";
import { P09JobDetail } from "../pages/P09JobDetail";
import { P10Report } from "../pages/P10Report";
import { P11Artifacts } from "../pages/P11Artifacts";
import { P12Inbox } from "../pages/P12Inbox";
import { P13Settings } from "../pages/P13Settings";
import { P14Drift } from "../pages/P14Drift";
import { Layout } from "./Layout";
import { createQueryClient } from "./queryClient";

export function App(): ReactNode {
  // 每个 App 实例一份 QueryClient：测试里挂载两次时不会共享缓存。
  const [client] = useState(createQueryClient);

  return (
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            {/* 全局 */}
            <Route path="/" element={<P01Projects />} />
            <Route path="/inbox" element={<P12Inbox />} />
            <Route path="/templates" element={<P03Templates />} />
            <Route path="/templates/import" element={<P05TemplateImport />} />
            <Route path="/templates/:templateId" element={<P04TemplateDetail />} />
            <Route path="/settings" element={<P13Settings />} />

            {/* 项目作用域 */}
            <Route path="/projects/:projectId">
              <Route index element={<P02Overview />} />
              <Route path="sources" element={<P06Sources />} />
              <Route path="config" element={<P07Config />} />
              <Route path="jobs" element={<P08Jobs />} />
              <Route path="jobs/:jobId" element={<P09JobDetail />} />
              <Route path="jobs/:jobId/report" element={<P10Report />} />
              <Route path="artifacts" element={<P11Artifacts />} />
              <Route path="drift" element={<P14Drift />} />
            </Route>

            {/* 未匹配：回到项目列表，而不是留在空白页 */}
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
