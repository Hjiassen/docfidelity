/** P07 决策点路由配置；适用范围与准入规则由服务端校验。 */
import type { ReactNode } from "react";

import type {
  DecisionAdmissionPolicy,
  DecisionLevel,
  DecisionPointRoute,
  DecisionRoute,
  DecisionStrategy,
  RoutingConfig,
} from "../api/types";
import { zh } from "../i18n/zh";
import { FORMAT_VERSION, missingBlock, SectionCard } from "./p07Sections";

const ROUTES: readonly DecisionRoute[] = ["rule", "small_model", "optional_large_model", "human"];
const STRATEGIES: readonly DecisionStrategy[] = ["rule_only", "rule_then_model", "model_then_rule"];
const LEVELS: readonly DecisionLevel[] = ["auto", "confirm", "manual"];
const ADMISSION_POLICIES: readonly DecisionAdmissionPolicy[] = ["require_confidence", "independent_validation"];
const FALLBACKS: readonly DecisionPointRoute["fallback"][] = ["rule_default", "skip", "queue"];

function emptyAsNull(text: string): string | null {
  return text.trim() === "" ? null : text.trim();
}

const NEW_DECISION_POINT: DecisionPointRoute = {
  admission_policy: "require_confidence",
  fallback: "rule_default",
  format_version: FORMAT_VERSION,
};

export function RoutingSection({
  routing,
  onChange,
}: {
  routing: RoutingConfig | undefined;
  onChange: (next: RoutingConfig) => void;
}): ReactNode {
  if (routing === undefined) {
    return <SectionCard title={zh.p07.secRouting}>{missingBlock()}</SectionCard>;
  }
  const points = Object.entries(routing.decision_points ?? {});

  const replace = (id: string, patch: Partial<DecisionPointRoute>): void => {
    const current = routing.decision_points?.[id] ?? NEW_DECISION_POINT;
    onChange({
      ...routing,
      decision_points: { ...routing.decision_points, [id]: { ...current, ...patch } },
    });
  };
  const rename = (from: string, to: string): void => {
    const entries = points.map(([key, value]) => [key === from ? to : key, value] as const);
    onChange({ ...routing, decision_points: Object.fromEntries(entries) });
  };

  return (
    <SectionCard
      title={zh.p07.secRouting}
      note={zh.p07.secRoutingNote}
      actions={
        <label className="row small" style={{ gap: 6, alignItems: "center" }}>
          <input
            type="checkbox"
            style={{ width: "auto" }}
            checked={routing.model_enabled}
            onChange={(event) => onChange({ ...routing, model_enabled: event.target.checked })}
          />
          <span>{zh.p07.routingModelEnabled}</span>
        </label>
      }
    >
      <p className="faint small">{zh.p07.routingModelNote}</p>

      <div className="row row--wrap" style={{ marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>{zh.p07.decisionPoints}</h3>
        <button
          type="button"
          className="btn btn--sm"
          onClick={() =>
            onChange({
              ...routing,
              decision_points: { ...routing.decision_points, "": NEW_DECISION_POINT },
            })
          }
        >
          {zh.p07.decisionAdd}
        </button>
      </div>

      {points.length === 0 ? (
        <p className="small muted" style={{ marginBottom: 0 }}>
          {zh.p07.decisionEmpty}
        </p>
      ) : (
        <div className="table-wrap">
          <table style={{ minWidth: 1380, tableLayout: "fixed" }}>
            <colgroup>
              {[220, 130, 140, 120, 170, 110, 240, 160, 90].map((width, index) => (
                <col key={index} style={{ width }} />
              ))}
            </colgroup>
            <thead>
              <tr>
                <th>{zh.p07.decisionPointId}</th>
                <th>{zh.p07.decisionRoute}</th>
                <th>{zh.p07.decisionStrategy}</th>
                <th>{zh.p07.decisionLevel}</th>
                <th>{zh.p07.decisionAdmissionPolicy}</th>
                <th>{zh.p07.decisionThreshold}</th>
                <th>{zh.p07.decisionFallback}</th>
                <th>{zh.p07.decisionModel}</th>
                <th>{zh.p07.colActions}</th>
              </tr>
            </thead>
            <tbody>
              {points.map(([id, route], index) => (
                <tr key={`${id}-${index}`}>
                  <td>
                    <input
                      className="mono small"
                      value={id}
                      onChange={(event) => rename(id, event.target.value)}
                    />
                  </td>
                  <td>
                    <select
                      value={route.route ?? ""}
                      onChange={(event) =>
                        replace(id, { route: (event.target.value || null) as DecisionRoute | null })
                      }
                    >
                      <option value="">{zh.p07.decisionInherit}</option>
                      {ROUTES.map((item) => (
                        <option key={item} value={item}>
                          {zh.p07.routeOf[item]}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <select
                      value={route.strategy ?? ""}
                      onChange={(event) =>
                        replace(id, {
                          strategy: (event.target.value || null) as DecisionStrategy | null,
                        })
                      }
                    >
                      <option value="">{zh.p07.decisionInherit}</option>
                      {STRATEGIES.map((item) => (
                        <option key={item} value={item}>
                          {zh.p07.strategyOf[item]}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <select
                      value={route.level ?? ""}
                      onChange={(event) =>
                        replace(id, { level: (event.target.value || null) as DecisionLevel | null })
                      }
                    >
                      <option value="">{zh.p07.decisionInherit}</option>
                      {LEVELS.map((item) => (
                        <option key={item} value={item}>
                          {zh.p07.levelOf[item]}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <select
                      aria-label={`${zh.p07.decisionAdmissionPolicy} ${id}`}
                      value={route.admission_policy ?? "require_confidence"}
                      onChange={(event) =>
                        replace(id, { admission_policy: event.target.value as DecisionAdmissionPolicy })
                      }
                    >
                      {ADMISSION_POLICIES.map((item) => (
                        <option key={item} value={item}>
                          {zh.p07.admissionPolicyOf[item]}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <input
                      type="number"
                      step="0.05"
                      min={0}
                      max={1}
                      value={route.confidence_threshold === null || route.confidence_threshold === undefined
                        ? ""
                        : String(route.confidence_threshold)}
                      onChange={(event) =>
                        replace(id, {
                          confidence_threshold:
                            event.target.value === "" ? null : Number(event.target.value),
                        })
                      }
                    />
                  </td>
                  <td>
                    <select
                      value={route.fallback}
                      onChange={(event) =>
                        replace(id, {
                          fallback: event.target.value as DecisionPointRoute["fallback"],
                        })
                      }
                    >
                      {FALLBACKS.map((item) => (
                        <option key={item} value={item}>
                          {zh.p07.fallbackOf[item]}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <input
                      className="mono small"
                      value={route.model ?? ""}
                      onChange={(event) => replace(id, { model: emptyAsNull(event.target.value) })}
                    />
                  </td>
                  <td>
                    <button
                      type="button"
                      className="btn btn--sm btn--danger"
                      onClick={() =>
                        onChange({
                          ...routing,
                          decision_points: Object.fromEntries(
                            points.filter(([key]) => key !== id),
                          ),
                        })
                      }
                    >
                      {zh.p07.docRemove}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="faint small" style={{ marginBottom: 0 }}>
        {zh.p07.thresholdNote}
      </p>
    </SectionCard>
  );
}
