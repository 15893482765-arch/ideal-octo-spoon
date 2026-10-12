import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// vitest globals 关闭时，Testing Library 的自动 cleanup 不会注册，需手动挂接
afterEach(() => {
  cleanup();
});
