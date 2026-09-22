import { describe, expect, it } from "vitest";
import { parse, router } from "../lib/router.svelte";

describe("router", () => {
  it("parses", () => {
    expect(parse("/")).toEqual({ name: "home" });
    expect(parse("/jobs/42")).toEqual({ name: "job", id: 42 });
    expect(parse("/jobs/42/")).toEqual({ name: "job", id: 42 });
    expect(parse("/jobs/abc")).toEqual({ name: "home" });
    expect(parse("/elsewhere")).toEqual({ name: "home" });
  });
  it("navigates and follows history", () => {
    router.go("/jobs/7");
    expect(location.pathname).toBe("/jobs/7");
    expect(router.route).toEqual({ name: "job", id: 7 });
    history.pushState(null, "", "/");
    dispatchEvent(new PopStateEvent("popstate"));
    expect(router.route).toEqual({ name: "home" });
  });
});
