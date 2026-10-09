/**
 * TanStack Query 的全局配置。
 *
 * 两条与后端语义绑定的设定：
 *
 * ① **4xx 不重试**。服务端把"请求本身不对"（422 字段错、415 类型拒、404 不存在）
 *    与"临时故障"都出成统一错误包，但**重试前者毫无意义**——重试三次只是把同一个
 *    错误说三遍。只有网络层失败（status 0）与 5xx 才值得退避重试。
 * ② **不做窗口聚焦重取**。作业状态由 SSE 推，页面不靠"用户切回来"来刷新；
 *    开着聚焦重取只会在用户来回切窗口时打出一串无谓请求。
 */

import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";

/** 网络层失败（无响应）与 5xx 值得重试；其余一律不重试。 */
function shouldRetry(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2) return false;
  if (!(error instanceof ApiError)) return false;
  return error.status === 0 || error.status >= 500;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        retryDelay: (attempt) => Math.min(500 * 2 ** attempt, 4000),
        refetchOnWindowFocus: false,
        staleTime: 0,
      },
      mutations: {
        retry: false,
      },
    },
  });
}
