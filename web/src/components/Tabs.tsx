/**
 * 页签条。P03 的两类资产（模板包 / 文档类型档案）与 P04 的八个契约分区共用一套。
 *
 * 为什么做**受控**组件、把选中态留在调用点
 * -------------------------------------
 * 页签选中态是**界面状态**：刷新后回到第一个页签是可接受的，谁也不会因为它丢了而
 * 误解数据。做成受控而不是带内部 state，是因为 P04 需要「点某个页签时顺便把滚动位置
 * 归零 / 触发一次按需请求」——受控之后这些都能在调用点一处表达。
 *
 * 为什么不写进 URL
 * --------------
 * 把「哪个页签」放进查询串会诱使下一处把它当成可分享的深链，而契约里没有这种东西；
 * 深链一旦出现就得回答「旧页签名下线后怎么办」，那是一笔没人要还的债。
 *
 * 语义是 `role="tablist"`：键盘可聚焦、屏幕阅读器能报出「第 n 个，共 m 个」，
 * 且 `aria-selected` 与视觉态同源（都读同一个 `value`），不会出现"看起来选中了、
 * 读出来没选中"。
 */

import type { ReactNode } from "react";

export interface TabItem<T extends string> {
  id: T;
  label: string;
}

export function Tabs<T extends string>({
  items,
  value,
  onChange,
}: {
  items: ReadonlyArray<TabItem<T>>;
  value: T;
  onChange: (id: T) => void;
}): ReactNode {
  return (
    <div className="tabs" role="tablist">
      {items.map((item) => {
        const active = item.id === value;
        return (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={active}
            className={active ? "tabs__item is-active" : "tabs__item"}
            onClick={() => onChange(item.id)}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
