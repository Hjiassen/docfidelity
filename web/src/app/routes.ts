/**
 * 路由常量（DD-14.2）。
 *
 * 为什么集中一处：站点地图在需求 7.3.2 定死了，页面之间互相跳转时若各自拼字符串，
 * 「改哪一侧、忘了同步另一侧」就会以"某个链接 404"的形式暴露。集中后，
 * 路径形状只在这里改。
 *
 * 页面 ↔ `FR-*` 的映射写在**各页面文件头部**（上游 6.7 的要求）。
 */

const enc = encodeURIComponent;

export const routes = {
  /* 全局 */
  p01Projects: "/",
  p12Inbox: "/inbox",
  p03Templates: "/templates",
  p04TemplateDetail: (templateId: string): string => `/templates/${enc(templateId)}`,
  p05TemplateImport: "/templates/import",
  p13Settings: "/settings",

  /* 项目作用域 */
  p02Overview: (projectId: string): string => `/projects/${enc(projectId)}`,
  p06Sources: (projectId: string): string => `/projects/${enc(projectId)}/sources`,
  p07Config: (projectId: string): string => `/projects/${enc(projectId)}/config`,
  p08Jobs: (projectId: string): string => `/projects/${enc(projectId)}/jobs`,
  p09JobDetail: (projectId: string, jobId: string): string =>
    `/projects/${enc(projectId)}/jobs/${enc(jobId)}`,
  p10Report: (projectId: string, jobId: string): string =>
    `/projects/${enc(projectId)}/jobs/${enc(jobId)}/report`,
  p11Artifacts: (projectId: string): string => `/projects/${enc(projectId)}/artifacts`,
  p14Drift: (projectId: string): string => `/projects/${enc(projectId)}/drift`,
} as const;

/** 项目作用域路由的前缀匹配（`Layout` 用它解析当前项目）。 */
export const PROJECT_SCOPE_PATTERN = "/projects/:projectId/*";
