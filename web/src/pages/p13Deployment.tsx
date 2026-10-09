/**
 * 部署形态与数据来源 —— 设置页的**一致性抽查**块（DD-18.1 / DD-13.7 / 验收 30/31）。
 *
 * 它回答的问题与上面的「数据边界」不同
 * ----------------------------------
 * 数据边界卡念的是**配置**（部署方式 / 模型开关 / 出口 / 存储根），来自 `GET /settings`；
 * 这一块念的是**这台机器此刻的运行现状**，来自 `GET /health`。两者必须对得上：
 * "配置说内网部署、进程却是公网实例"这类错位，只有把两边摆在一起才看得出来。
 *
 * 为什么这两条事实在 `SettingsView` 里找不到
 * ----------------------------------------
 * `worker_mode`（谁在消费作业）与 `demo_seed`（库里的示例是怎么来的）是**运行结果**，
 * 不是可改配置；`PUT /settings` 改不动它们。放进 `SettingsView` 会让人以为可以改。
 * 它们只出现在探针里——顺带的好处是：**探针不通时这一块会明说读不到**，而不是拿一份
 * 缓存里的旧配置冒充现状。
 *
 * 「external 不等于有人真的在跑」
 * ---------------------------
 * `worker_mode=external` 只说明**本进程不消费**，消费由独立服务承担。这台机器上
 * `df-worker` 起没起，`/health` 答不了（那要问编排器）。界面**如实这么写**，不把
 * "配置成外部"渲染成"外部进程健在"——后者是一句无法核对的结论。
 */

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { systemApi } from "../api/endpoints";
import { ErrorPanel, KeyValues, Loading } from "../components/primitives";
import { zh } from "../i18n/zh";

/** 首启装载：三种结局各自说什么。**跳过要说明原因**，否则读起来像失败。 */
function SeedRows({ seed }: { seed: { skipped: boolean; seeded: string[]; errors: string[] } }): ReactNode {
  return (
    <>
      <p className="small" style={{ marginTop: 8, marginBottom: 0 }}>
        <span className="muted">{zh.p13.deploymentSeedState}：</span>
        {seed.skipped ? (
          <>
            <span className="tag tag--neutral">{zh.p13.deploymentSeedSkipped}</span>{" "}
            {zh.p13.deploymentSeedSkippedWhy}
          </>
        ) : (
          <span className="tag tag--ok">{zh.p13.deploymentSeedDone(seed.seeded.length)}</span>
        )}
      </p>

      {seed.seeded.length > 0 && (
        <ul className="small mono" style={{ marginTop: 4, marginBottom: 0 }}>
          {seed.seeded.map((ref) => (
            <li key={ref}>{ref}</li>
          ))}
        </ul>
      )}

      {seed.errors.length > 0 && (
        <>
          <p className="small" style={{ marginTop: 8, marginBottom: 0 }}>
            {zh.p13.deploymentSeedErrors}
          </p>
          <ul className="small mono" style={{ marginTop: 4, marginBottom: 0 }}>
            {seed.errors.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}

export function DeploymentCard(): ReactNode {
  // 与顶部布局共用同一个 queryKey：一次请求，两处读同一份事实（不会互相矛盾），
  // 也顺带获得布局那份 30s 轮询。
  const health = useQuery({
    queryKey: ["health"],
    queryFn: systemApi.health,
    retry: false,
  });

  const data = health.data;

  return (
    <div className="card card--quiet">
      <h2>{zh.p13.deploymentTitle}</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        {zh.p13.deploymentIntro}
      </p>
      <p className="faint small" style={{ marginTop: 0 }}>
        {zh.p13.deploymentSource}
      </p>

      {health.isPending && <Loading what={zh.p13.deploymentTitle} />}
      {health.isError && (
        <>
          <p className="small">{zh.p13.deploymentUnreadable}</p>
          <ErrorPanel error={health.error} />
        </>
      )}

      {data !== undefined && (
        <>
          <KeyValues
            items={[
              {
                label: zh.p13.deploymentWorkerMode,
                value: (
                  <>
                    <span className="tag tag--neutral">
                      {data.worker_mode === "in-process"
                        ? zh.p13.deploymentWorkerInProcess
                        : zh.p13.deploymentWorkerExternal}
                    </span>{" "}
                    <span className="faint small">
                      {data.worker_mode === "in-process"
                        ? zh.p13.deploymentWorkerInProcessNote
                        : zh.p13.deploymentWorkerExternalNote}
                    </span>
                  </>
                ),
              },
              {
                label: zh.p13.deploymentStages,
                value:
                  data.stages.missing.length === 0 ? (
                    <span className="tag tag--ok">{zh.p13.deploymentStagesOk(data.stages.registered.length)}</span>
                  ) : (
                    <>
                      <span className="tag tag--danger">
                        {zh.p13.deploymentStagesMissing(data.stages.missing.length)}
                      </span>{" "}
                      <span className="mono small">{data.stages.missing.join(", ")}</span>
                    </>
                  ),
              },
              { label: zh.p13.deploymentRepository, value: <span className="mono small">{data.repository}</span> },
              { label: zh.p13.deploymentStorageRoot, value: <span className="mono small">{data.storage_root}</span> },
            ]}
          />

          {data.demo_seed === null ? (
            <p className="small" style={{ marginBottom: 0 }}>
              <span className="muted">{zh.p13.deploymentSeedState}：</span>
              <span className="tag tag--neutral">{zh.p13.deploymentSeedOff}</span>{" "}
              {zh.p13.deploymentSeedOffNote}
            </p>
          ) : (
            <SeedRows seed={data.demo_seed} />
          )}

          <p className="faint small" style={{ marginBottom: 0 }}>
            {zh.p13.deploymentCheckNote}
          </p>
        </>
      )}
    </div>
  );
}
