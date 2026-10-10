import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

import { resetBrowserSession } from "./utils";

// jsdom has no layout, so it does not implement scrolling; tests spy on this no-op.
Element.prototype.scrollIntoView = () => undefined;

afterEach(() => {
  cleanup();
  resetBrowserSession();
  window.sessionStorage.clear();
  window.localStorage.clear();
});
