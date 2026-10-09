import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/App";
import "./styles.css";

const container = document.getElementById("root");
if (container === null) {
  // 挂载点缺失是构建/模板问题，不该静默变成一个白屏。
  throw new Error("找不到挂载点 #root：请检查 index.html");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
