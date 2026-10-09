/**
 * 数据边界的**三条可核对事实**（DD-13.7 / NFR-21 / 验收 30）。
 *
 * 为什么单独成一个组件：这三条在 `deploy/README.md` 第 2 节已经写死一份，若在设置页与
 * 项目总览各抄一遍，就会出现"部署文档说一条、界面说三条"的漂移——而这一区的全部价值
 * 就在于"说法与事实一致"。**文案同源、布局各自**（与 P02 复用 `zh.p13.boundary*`
 * 是同一条纪律）。
 *
 * 三条各带一个"核对方式"，这是刻意的：一段只声明"不会外发"的话术不可核对，也就没人会去
 * 核对；给出接口或文件之后，"这句话对不对"变成一个当场能做完的动作。
 *
 * **界面不判断**（NFR-22）：每一项的当前值原样来自服务端（`GET /settings` 的
 * `data_boundary`），界面既不归一化也不补默认值——补出来的值看起来一样，但那是猜的。
 */

import type { ReactNode } from "react";

import type { DataBoundary } from "../api/types";
import { zh } from "../i18n/zh";

export function DataBoundaryFacts({ boundary }: { boundary: DataBoundary }): ReactNode {
  const egress = boundary.model_base_url;
  const hasEgress = egress !== null && egress !== undefined && egress !== "";

  return (
    <div className="card card--quiet" style={{ marginTop: 12 }}>
      <h3 className="small">{zh.boundaryFacts.title}</h3>
      <p className="faint small" style={{ marginTop: 0 }}>
        {zh.boundaryFacts.intro}
      </p>
      <dl className="grid grid--kv">
        <div style={{ display: "contents" }}>
          <dt>
            {zh.boundaryFacts.volumeTitle}
            <div className="faint small" style={{ fontWeight: 400 }}>
              {zh.boundaryFacts.volumeHow}
            </div>
          </dt>
          <dd className="mono small">{boundary.storage_path ?? zh.app.none}</dd>
        </div>
        <div style={{ display: "contents" }}>
          <dt>
            {zh.boundaryFacts.egressTitle}
            <div className="faint small" style={{ fontWeight: 400 }}>
              {zh.boundaryFacts.egressHow}
            </div>
          </dt>
          <dd>
            {hasEgress ? (
              <span className="mono small">{egress}</span>
            ) : (
              <span className="tag tag--ok">{zh.boundaryFacts.egressNone}</span>
            )}
          </dd>
        </div>
        <div style={{ display: "contents" }}>
          <dt>
            {zh.boundaryFacts.contentTitle}
            <div className="faint small" style={{ fontWeight: 400 }}>
              {zh.boundaryFacts.contentHow}
            </div>
          </dt>
          <dd className="small">{zh.boundaryFacts.contentValue}</dd>
        </div>
      </dl>
    </div>
  );
}
