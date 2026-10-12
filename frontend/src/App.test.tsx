import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import App from "./App";

describe("App（T01 冒烟）", () => {
  it("渲染平台名称", () => {
    render(<App />);
    expect(screen.getByText("沁爽饮料分销管理平台")).toBeInTheDocument();
  });

  it("空态提供下一步指引", () => {
    render(<App />);
    expect(screen.getByText(/下一步/)).toBeInTheDocument();
  });
});
