/**
 * P01 项目列表 —— 对应需求 **FR-M7-10**；入口 `/`（站点地图 7.3.2）。
 *
 * 目的：选择进入哪个项目的工作区。
 *
 * 空态必须给**两条路**（新建空项目 / 一键创建演示项目），这是 10.10「一键可体验」
 * 在界面上的落点——空态只给一条路，第一次打开的人就只能在"自己配"和"什么都没有"
 * 之间选，而演示项目正是用来消除"这东西到底能干什么"的疑问的。
 *
 * 已实现字段与未实现字段
 * ----------------------
 * 规格里的卡片内容包含"模板包数 / 事实源数 / 最近作业状态 / 存储占用"。这些需要
 * P03 / P06 的检索端点与一个**按项目汇总**的端点，而它们不在 P0-15 首批范围内
 * （P08 的作业列表接口已可用，但卡片要的是汇总值，不是列表）。本轮卡片展示
 * 服务端确实给了的字段（名称、创建/更新时间、演示与归档标记），**不编造**其它数字
 * ——一个灰色的"0 个模板包"会被读成"这个项目没有模板"，而事实是"没查"。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import { projectsApi } from "../api/endpoints";
import type { ProjectOut } from "../api/types";
import { EmptyState, ErrorPanel, Loading, formatTime } from "../components/primitives";
import { routes } from "../app/routes";
import { zh } from "../i18n/zh";

function ProjectCard({
  project,
  onRename,
  onToggleArchive,
  busy,
}: {
  project: ProjectOut;
  onRename: (projectId: string, name: string) => void;
  onToggleArchive: (projectId: string, archived: boolean) => void;
  busy: boolean;
}): ReactNode {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(project.name);

  return (
    <div className="card">
      <div className="row">
        {editing ? (
          <input
            autoFocus
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && draft.trim() !== "") {
                onRename(project.project_id, draft.trim());
                setEditing(false);
              }
              if (event.key === "Escape") setEditing(false);
            }}
          />
        ) : (
          <h3 style={{ margin: 0, fontSize: 15 }}>{project.name}</h3>
        )}
        {project.demo && <span className="tag tag--info">{zh.p01.demo}</span>}
        {project.archived && <span className="tag tag--neutral">{zh.p01.archived}</span>}
      </div>

      <dl className="grid grid--kv small" style={{ marginTop: 12 }}>
        <dt>{zh.generic.createdAt}</dt>
        <dd>{formatTime(project.created_at)}</dd>
        <dt>最近更新</dt>
        <dd>{formatTime(project.updated_at)}</dd>
      </dl>

      <div className="row row--wrap" style={{ marginTop: 14 }}>
        <Link className="btn btn--primary btn--sm" to={routes.p02Overview(project.project_id)}>
          {zh.p01.open}
        </Link>
        <button
          type="button"
          className="btn btn--sm"
          disabled={busy}
          onClick={() => {
            setDraft(project.name);
            setEditing(!editing);
          }}
        >
          {zh.p01.rename}
        </button>
        <button
          type="button"
          className="btn btn--sm"
          disabled={busy}
          onClick={() => onToggleArchive(project.project_id, !project.archived)}
        >
          {project.archived ? zh.p01.unarchive : zh.p01.archive}
        </button>
      </div>
    </div>
  );
}

export function P01Projects(): ReactNode {
  const [includeArchived, setIncludeArchived] = useState(false);
  const [name, setName] = useState("");
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const list = useQuery({
    queryKey: ["projects", includeArchived],
    queryFn: () => projectsApi.list(includeArchived),
  });

  const invalidate = (): void => {
    void queryClient.invalidateQueries({ queryKey: ["projects"] });
  };

  const create = useMutation({
    mutationFn: (input: { name: string; demo: boolean }) =>
      projectsApi.create(input.name, input.demo),
    onSuccess: (project) => {
      setName("");
      invalidate();
      navigate(routes.p02Overview(project.project_id));
    },
  });

  const archive = useMutation({
    mutationFn: (input: { projectId: string; archived: boolean }) =>
      projectsApi.setArchived(input.projectId, input.archived),
    onSuccess: invalidate,
  });

  const rename = useMutation({
    mutationFn: (input: { projectId: string; name: string }) =>
      projectsApi.rename(input.projectId, input.name),
    onSuccess: invalidate,
  });

  const projects = list.data ?? [];
  const busy = create.isPending || archive.isPending || rename.isPending;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{zh.p01.title}</h1>
          <p>{zh.p01.subtitle}</p>
        </div>
        <div className="page-head__actions">
          <label className="row small" style={{ width: "auto" }}>
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(event) => setIncludeArchived(event.target.checked)}
              style={{ width: "auto" }}
            />
            {zh.p01.showArchived}
          </label>
        </div>
      </div>

      {create.isError && <ErrorPanel error={create.error} />}
      {(archive.isError || rename.isError) && (
        <ErrorPanel error={archive.error ?? rename.error} />
      )}

      <div className="card">
        <h2>{zh.p01.newProject}</h2>
        <form
          className="row"
          onSubmit={(event) => {
            event.preventDefault();
            if (name.trim() === "") return;
            create.mutate({ name: name.trim(), demo: false });
          }}
        >
          <label className="field" style={{ flex: 1 }}>
            <span>{zh.p01.name}</span>
            <input
              value={name}
              placeholder={zh.p01.namePlaceholder}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <button
            type="submit"
            className="btn btn--primary"
            disabled={busy || name.trim() === ""}
            style={{ alignSelf: "flex-end" }}
          >
            {zh.p01.newProject}
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy}
            style={{ alignSelf: "flex-end" }}
            onClick={() => create.mutate({ name: zh.p01.demoProject, demo: true })}
          >
            {zh.p01.demoProject}
          </button>
        </form>
      </div>

      {list.isPending && <Loading />}
      {list.isError && <ErrorPanel error={list.error} />}

      {list.isSuccess && projects.length === 0 && (
        <EmptyState
          title={zh.p01.emptyTitle}
          body={zh.p01.emptyBody}
          action={
            <button
              type="button"
              className="btn btn--primary"
              disabled={busy}
              onClick={() => create.mutate({ name: zh.p01.demoProject, demo: true })}
            >
              {zh.p01.demoProject}
            </button>
          }
        />
      )}

      {projects.length > 0 && (
        <div className="grid grid--cards">
          {projects.map((project) => (
            <ProjectCard
              key={project.project_id}
              project={project}
              busy={busy}
              onRename={(projectId, next) => rename.mutate({ projectId, name: next })}
              onToggleArchive={(projectId, archived) => archive.mutate({ projectId, archived })}
            />
          ))}
        </div>
      )}

      <p className="faint small">{zh.p01.archivedNote}</p>
    </>
  );
}
