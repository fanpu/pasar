import "@testing-library/jest-dom/vitest";

// jsdom has no layout engine and no ResizeObserver. Components that use `bind:clientWidth`
// (e.g. Timeline) need at least a no-op implementation to mount; since jsdom never resizes
// anything, the callback is simply never invoked and the bound value stays 0.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
