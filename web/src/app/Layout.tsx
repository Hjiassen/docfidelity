/**
 * 全局布局：顶部栏 + 左侧导航 + 主工作区（7.3.1 / DD-14.1）。
 *
 * 顶部栏承载**跨项目**的东西（项目切换、作业指示器、待裁决徽标、设置），
 * 左侧导航承载**项目作用域内**的页面。这条分工来自 7.3.1：跨项目的东西进顶部，
 * 项目的东西进左侧。
 *
 * 顶部提示条只放**状态型警示**（降级、模型关闭、服务重启导致作业中断），
 * 普通提示用行内反馈——把两者混在一起，结果就是使用者对所有提示都失去敏感度。
 */

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { NavLink, Outlet, useMatch, useNavigate } from "react-router-dom";

import { projectsApi, systemApi } from "../api/endpoints";
import { DecisionBadge } from "../components/DecisionBadge";
import { JobIndicator } from "../components/JobIndicator";
import { zh } from "../i18n/zh";
import { PROJECT_SCOPE_PATTERN, routes } from "./routes";

function NavItem({ to, label, end = false }: { to: string; label: string; end?: boolean }): ReactNode {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) => (isActive ? "sidenav__item is-active" : "sidenav__item")}
    >
      {label}
    </NavLink>
  );
}

/** 顶部栏的警示条。只表达"服务端告诉我的状态"，不做前端判断。 */
function StatusBanner({ buildReady, modelEnabled }: { buildReady: boolean; modelEnabled: boolean }): ReactNode {
  if (!buildReady) {
    return <div className="banner">{zh.layout.bannerStagesMissing}</div>;
  }
  if (!modelEnabled) {
    return <div className="banner">{zh.layout.bannerModelOff}</div>;
  }
  return null;
}

export function Layout(): ReactNode {
  const match = useMatch(PROJECT_SCOPE_PATTERN);
  const projectId = match?.params["projectId"] ?? null;
  const navigate = useNavigate();

  const { data: health } = useQuery({
    queryKey: ["health"],
    queryFn: systemApi.health,
    refetchInterval: 30_000,
    // 探针失败不该重试：它失败就等于"后端不在"，重试三次只是延迟把这件事说出来。
    retry: false,
  });

  const { data: projects } = useQuery({
    queryKey: ["projects", "for-nav"],
    queryFn: () => projectsApi.list(false),
  });

  return (
    <div className="shell">
      <header className="topbar">
        <NavLink to={routes.p01Projects} className="topbar__brand">
          {zh.app.name}
        </NavLink>

        {projects !== undefined && projects.length > 0 && (
          <select
            aria-label={zh.layout.switchProject}
            value={projectId ?? ""}
            onChange={(event) => {
              const next = event.target.value;
              if (next !== "") navigate(routes.p02Overview(next));
            }}
            style={{ width: 200 }}
          >
            <option value="">{zh.layout.switchProject}</option>
            {projects.map((project) => (
              <option key={project.project_id} value={project.project_id}>
                {project.name}
              </option>
            ))}
          </select>
        )}

        <span className="topbar__spacer" />

        <JobIndicator />
        <DecisionBadge />
        <NavLink to={routes.p13Settings} className="sidenav__item" style={{ padding: "4px 10px" }}>
          {zh.nav.settings}
        </NavLink>
      </header>

      <nav className="sidenav">
        <div className="sidenav__group">{zh.nav.scopeGlobal}</div>
        <NavItem to={routes.p01Projects} label={zh.nav.projects} end />
        {/* 裁决待办是**跨项目**的页（7.3.1）：顶部徽标是快捷入口，导航里也要有一条。 */}
        <NavItem to={routes.p12Inbox} label={zh.nav.inbox} />
        <NavItem to={routes.p03Templates} label={zh.nav.templates} />
        <NavItem to={routes.p05TemplateImport} label={zh.p05.title} />
        <NavItem to={routes.p13Settings} label={zh.nav.settings} />

        {projectId !== null && (
          <>
            <div className="sidenav__group">{zh.nav.scopeProject}</div>
            <NavItem to={routes.p02Overview(projectId)} label={zh.nav.overview} end />
            <NavItem to={routes.p06Sources(projectId)} label={zh.nav.sources} />
            <NavItem to={routes.p07Config(projectId)} label={zh.nav.config} />
            <NavItem to={routes.p08Jobs(projectId)} label={zh.nav.jobs} />
            <NavItem to={routes.p11Artifacts(projectId)} label={zh.nav.artifacts} />
            <NavItem to={routes.p14Drift(projectId)} label={zh.nav.drift} />
          </>
        )}
      </nav>

      <main className="main">
        <StatusBanner
          buildReady={health?.build_ready ?? true}
          modelEnabled={health?.model_enabled ?? true}
        />
        <Outlet />
      </main>
    </div>
  );
}
