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

  it("go() keeps the current search when the path has no '?'", () => {
    history.pushState(null, "", "/?tag=x");
    dispatchEvent(new PopStateEvent("popstate"));
    router.go("/jobs/3");
    expect(location.pathname).toBe("/jobs/3");
    expect(location.search).toBe("?tag=x");
    expect(router.route).toEqual({ name: "job", id: 3 });
    expect(router.search).toBe("?tag=x");
  });

  it("go() replaces the search when the path has a '?'", () => {
    history.pushState(null, "", "/?tag=x");
    dispatchEvent(new PopStateEvent("popstate"));
    router.go("/?tag=y");
    expect(location.pathname).toBe("/");
    expect(location.search).toBe("?tag=y");
    expect(router.search).toBe("?tag=y");
  });

  it("setSearch changes the search and keeps the route", () => {
    router.go("/jobs/9");
    router.setSearch("?state=failed");
    expect(location.pathname).toBe("/jobs/9");
    expect(location.search).toBe("?state=failed");
    expect(router.route).toEqual({ name: "job", id: 9 });
    expect(router.search).toBe("?state=failed");
  });

  it("setSearch(search, true) uses replaceState instead of pushState", () => {
    router.go("/");
    const before = history.length;
    router.setSearch("?q=x", true);
    expect(location.search).toBe("?q=x");
    expect(history.length).toBe(before);
    router.setSearch("?q=y");
    expect(history.length).toBe(before + 1);
  });

  it("popstate updates both route and search", () => {
    router.go("/jobs/1");
    history.pushState(null, "", "/jobs/2?tag=z");
    dispatchEvent(new PopStateEvent("popstate"));
    expect(router.route).toEqual({ name: "job", id: 2 });
    expect(router.search).toBe("?tag=z");
  });
});
