/**
 * 待裁决徽标（7.5.2）。
 *
 * **不可隐藏**：漏裁决会直接导致"带病定稿"，这是本项目最不希望发生的交付事故。
 * 所以 0 的时候也要显示（显示成"无待裁决"），而不是让徽标消失——消失意味着
 * "没有这个问题"，而事实是"当前没有"。
 *
 * 计数口径
 * --------
 * 数字取自 `GET /inbox` 的 `count`，即**未办结的待裁决项数**（跨作业、跨项目）。
 * 它与 `GET /jobs?status=awaiting_decision` 的**作业数不相等**（一个作业可能有多条
 * 待裁决项），故不能用后者代替：那会让徽标数字偏小而永远清不到零。
 *
 * 这一处在 P0-15 首批曾降级为"待裁决作业数"（当时 `/inbox` 尚未落地，原注释写明
 * "等 `/inbox` 落地后替换 `queryFn` 即可"）。端点随 P0-15 第 2 轮到岸，故已换。
 */

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { inboxApi } from "../api/endpoints";
import { routes } from "../app/routes";
import { zh } from "../i18n/zh";

/** 30s 轮询（DD-14.3）。作业事件也会触发页面重取，故不必更密。 */
const POLL_MS = 30_000;

export function DecisionBadge(): ReactNode {
  const { data } = useQuery({
    queryKey: ["inbox", "count"],
    queryFn: () => inboxApi.list(),
    refetchInterval: POLL_MS,
  });

  // `count` 由服务端给出，界面不做业务计算（NFR-22）。
  const count = data?.count ?? 0;
  const alert = count > 0;

  return (
    <Link
      className={alert ? "badge-count badge-count--alert" : "badge-count"}
      to={routes.p12Inbox}
      title={zh.nav.inbox}
    >
      <span>{zh.nav.inbox}</span>
      <span className="strong">{count}</span>
    </Link>
  );
}
