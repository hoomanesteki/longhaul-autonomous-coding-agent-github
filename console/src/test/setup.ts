import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Testing Library only cleans up on its own when the test globals are on its path; doing it here
// keeps one test's DOM from leaking into the next.
afterEach(() => {
  cleanup();
});
