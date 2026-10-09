/**
 * HTTP 客户端：把服务端的**统一错误包**翻译成前端的 `ApiError`。
 *
 * 为什么值得单独一层：服务端所有失败（内核异常、HTTP 层失败、请求体校验失败、
 * 兜底异常）都出成同一个 JSON 结构（DD-13.3），所以界面上**只需要处理一种错误形状**。
 * 如果每个调用点自己 `if (!res.ok) throw new Error(await res.text())`，
 * 那个统一结构就被丢掉了一半——`remediation`（可以做什么）与 `trace_id`（怎么查）
 * 恰恰是使用者最需要的两段。
 *
 * 纪律：本模块**不解释错误码的业务含义**。错误码 → HTTP 状态由服务端定，
 * 文案由服务端给；这里只做结构与传输层的翻译（14.5「界面不承载业务判断」）。
 */

import { zh } from "../i18n/zh";

/** 所有接口的前缀。开发期由 vite 代理转发到后端，故前端只认相对路径。 */
export const API_PREFIX = "/api/v1";

/** 服务端在响应头里回带的追踪标识。 */
export const TRACE_HEADER = "X-Trace-Id";

/** 网络层失败的本地错误码（不是服务端枚举里的值，故用 `E_` 之外的前缀避免混淆）。 */
export const NETWORK_ERROR_CODE = "LOCAL_NETWORK";

/**
 * 服务端统一错误包。
 *
 * 字段名与 `server/errors.py` 的 `ErrorEnvelope` **逐字对应**——两边任一改动都要同步，
 * 故这里刻意不写成"可选字段"来容忍漂移：宁可类型报错，也不要运行时静默丢字段。
 */
export interface ErrorEnvelope {
  code: string;
  message: string;
  where: Record<string, unknown>;
  remediation: string[];
  trace_id: string;
}

/**
 * 界面层错误。
 *
 * 三段式对齐 14.5 的要求：发生了什么 / 影响什么 / 可以做什么。
 * `where` 就是"影响什么"的载体——它是服务端给出的**结构化定位**（哪个字段、
 * 哪条记录、哪个阶段），前端只负责展示，不做推断。
 */
export class ApiError extends Error {
  /** HTTP 状态码；网络层失败时为 0。 */
  readonly status: number;
  readonly code: string;
  readonly where: Record<string, unknown>;
  readonly remediation: readonly string[];
  readonly traceId: string;

  constructor(init: {
    status: number;
    code: string;
    message: string;
    where?: Record<string, unknown>;
    remediation?: readonly string[];
    traceId?: string;
  }) {
    super(init.message);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.where = init.where ?? {};
    this.remediation = init.remediation ?? [];
    this.traceId = init.traceId ?? "";
  }

  /** 第一段：发生了什么。 */
  get happened(): string {
    return this.message;
  }

  /** 第二段：影响什么。有定位就展示定位，没有就给一句中性的"未生效"。 */
  get impact(): string {
    return Object.keys(this.where).length > 0 ? zh.error.impactLocated : zh.error.impactDefault;
  }

  /** 第三段：可以做什么。 */
  get actions(): readonly string[] {
    return this.remediation;
  }

  /** 结构化的 `where` 摊平成可展示的行。 */
  get locatedItems(): ReadonlyArray<{ key: string; value: string }> {
    return Object.entries(this.where).map(([key, value]) => ({
      key,
      value: typeof value === "string" ? value : JSON.stringify(value),
    }));
  }
}

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface RequestOptions {
  method?: HttpMethod;
  /** JSON 请求体；与 `form` 互斥。 */
  json?: unknown;
  /** 表单请求体（文件上传）；与 `json` 互斥。 */
  form?: FormData;
  signal?: AbortSignal;
  /** 追加查询参数；`undefined` / `null` 的项会被丢掉。 */
  query?: Record<string, string | number | boolean | undefined | null>;
  /**
   * 不加 `/api/v1` 前缀。
   *
   * 只有 `/health` 用得上——它是服务级探针，不属于版本化的业务接口，
   * 刻意放在根路径上（DD-13.2）。用它的时候别顺手把业务接口也改成绝对路径。
   */
  absolute?: boolean;
}

/** 拼出可用的请求地址（也供 SSE 之类的非 fetch 场景使用）。 */
export function apiUrl(path: string, query?: RequestOptions["query"]): string {
  const base = path.startsWith("/") ? path : `/${path}`;
  return withQuery(`${API_PREFIX}${base}`, query);
}

/** 在地址后追加查询串；空值一律丢掉，不用 `?a=&b=` 这种噪音。 */
export function withQuery(url: string, query?: RequestOptions["query"]): string {
  if (query === undefined) return url;
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs === "" ? url : `${url}?${qs}`;
}

/** 把响应体尽力解析成错误包；解析不出来时给一个**保留原文**的兜底错误。 */
async function toApiError(response: Response): Promise<ApiError> {
  const traceId = response.headers.get(TRACE_HEADER) ?? "";
  let text = "";
  try {
    text = await response.text();
  } catch {
    text = "";
  }
  if (text !== "") {
    try {
      const parsed: unknown = JSON.parse(text);
      if (isEnvelope(parsed)) {
        return new ApiError({
          status: response.status,
          code: parsed.code,
          message: parsed.message,
          where: parsed.where,
          remediation: parsed.remediation,
          traceId: parsed.trace_id === "" ? traceId : parsed.trace_id,
        });
      }
    } catch {
      // 落到下面的兜底：宁可把原文给人看，也不要编一句"未知错误"。
    }
  }
  const fallback = text === "" ? `${response.status} ${response.statusText}` : text;
  return new ApiError({
    status: response.status,
    code: `HTTP_${response.status}`,
    message: fallback,
    traceId,
  });
}

function isEnvelope(value: unknown): value is ErrorEnvelope {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record["code"] === "string" && typeof record["message"] === "string";
}

function toNetworkError(cause: unknown): ApiError {
  const detail = cause instanceof Error ? cause.message : String(cause);
  return new ApiError({
    status: 0,
    code: NETWORK_ERROR_CODE,
    message: `${zh.error.networkTitle}：${detail}`,
    remediation: [zh.error.networkDetail],
  });
}

/**
 * 发一次请求。
 *
 * 返回 `undefined`（而非抛错）的唯一情形是服务端回了空体（204）；其它非 2xx 一律抛
 * `ApiError`，让调用点只需处理成功分支。
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  let body: BodyInit | undefined;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.json);
  } else if (options.form !== undefined) {
    // 刻意不设 Content-Type：multipart 的 boundary 必须由浏览器自己写。
    body = options.form;
  }

  let response: Response;
  try {
    const url =
      options.absolute === true ? withQuery(path, options.query) : apiUrl(path, options.query);
    response = await fetch(url, {
      method: options.method ?? "GET",
      headers,
      ...(body === undefined ? {} : { body }),
      ...(options.signal === undefined ? {} : { signal: options.signal }),
    });
  } catch (cause) {
    throw toNetworkError(cause);
  }

  if (!response.ok) {
    throw await toApiError(response);
  }

  if (response.status === 204 || response.headers.get("Content-Length") === "0") {
    return undefined as T;
  }
  const text = await response.text();
  if (text === "") {
    return undefined as T;
  }
  return JSON.parse(text) as T;
}

/** 产物/文件的下载直链（`<a download>` 用，不经过 fetch）。 */
export function downloadUrl(artifactId: string): string {
  return apiUrl(`/artifacts/${encodeURIComponent(artifactId)}/download`);
}

/**
 * 模板包导出的下载直链（`<a download>` 用）。
 *
 * 导出走的是**浏览器自己的下载**而不是 `fetch`：拿 zip 字节再拼 `Blob` 会先把整包
 * 读进内存、还要自己维护对象 URL 的释放时机，而 `<a download>` 由浏览器直接落盘，
 * 顺带还能吃到服务端给的那个 `Content-Disposition` 文件名。
 */
export function templatePackageUrl(templateId: string, version?: string): string {
  return apiUrl(`/templates/${encodeURIComponent(templateId)}/package`, { version });
}

/** 档案包导出的下载直链（同上）。 */
export function profileExportUrl(profileId: string, version?: string): string {
  return apiUrl(`/profiles/${encodeURIComponent(profileId)}/export`, { version });
}

/**
 * 某次作业的**产物整包**下载直链（P11 的「整套下载」）。
 *
 * 整包由服务端打（`platform.fs.zip_directory`，条目排序 + 时刻钉死），而不是前端逐份
 * 下载再自己压缩：后者会把"哪些文件算产物"这条判定复制到浏览器里，而它已经含一条
 * 业务结论（工作区里的 `inputs/` / `checkpoints/` / 事件日志**不算**产物）。
 */
export function artifactPackageUrl(jobId: string): string {
  return apiUrl(`/jobs/${encodeURIComponent(jobId)}/artifacts/package`);
}

/** 事件的 SSE 地址。 */
export function eventsUrl(jobId: string): string {
  return apiUrl(`/jobs/${encodeURIComponent(jobId)}/events`);
}

/**
 * 把一段文本**在浏览器本地**落盘。
 *
 * 用于服务端还没有导出端点的那几处（P07 的配置导出、P10 的报告/豁免清单、P09 的事件流）。
 * 三处各自写一遍 `Blob` + `URL.createObjectURL` + `revokeObjectURL` 的风险很具体：
 * 漏掉 `revokeObjectURL` 会把整段内容留在内存里直到页面关闭，而这类泄漏没有任何可见症状。
 *
 * 纪律：调用点必须**自己**在文件名/文案里说明这是本地导出——`<a download>` 落下来的字节
 * 与服务端给的导出件长得一样，不说清会被当成服务端产物（那是审计上很糟的一种混淆）。
 */
export function downloadText(filename: string, text: string, mime: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}
