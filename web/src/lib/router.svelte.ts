export type Route = { name: "home" } | { name: "job"; id: number };

const JOB_PATH = /^\/jobs\/(\d+)\/?$/;

export function parse(pathname: string): Route {
  const m = JOB_PATH.exec(pathname);
  return m ? { name: "job", id: Number(m[1]) } : { name: "home" };
}

class Router {
  #route = $state<Route>(parse(location.pathname));
  #search = $state<string>(location.search);

  get route(): Route {
    return this.#route;
  }

  get search(): string {
    return this.#search;
  }

  /** `path` may include a `?...` query string; if it doesn't, the current search is kept (so
   * opening/closing a job preserves the filter without every caller having to thread it through). */
  go(path: string): void {
    const i = path.indexOf("?");
    const pathname = i === -1 ? path : path.slice(0, i);
    const search = i === -1 ? this.#search : path.slice(i);
    history.pushState(null, "", pathname + search);
    this.#route = parse(pathname);
    this.#search = search;
  }

  /** Changes only the query string, keeping the current pathname. `replace` uses replaceState
   * (for typing in search, so it doesn't spam history) instead of pushState (for chip clicks). */
  setSearch(search: string, replace = false): void {
    const url = location.pathname + search;
    if (replace) history.replaceState(null, "", url);
    else history.pushState(null, "", url);
    this.#search = search;
  }

  constructor() {
    addEventListener("popstate", () => {
      this.#route = parse(location.pathname);
      this.#search = location.search;
    });
  }
}

export const router: {
  readonly route: Route;
  readonly search: string;
  go(path: string): void;
  setSearch(search: string, replace?: boolean): void;
} = new Router();
