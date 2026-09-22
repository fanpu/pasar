export type Route = { name: "home" } | { name: "job"; id: number };

const JOB_PATH = /^\/jobs\/(\d+)\/?$/;

export function parse(pathname: string): Route {
  const m = JOB_PATH.exec(pathname);
  return m ? { name: "job", id: Number(m[1]) } : { name: "home" };
}

class Router {
  #route = $state<Route>(parse(location.pathname));

  get route(): Route {
    return this.#route;
  }

  go(path: string): void {
    history.pushState(null, "", path);
    this.#route = parse(path);
  }

  constructor() {
    addEventListener("popstate", () => {
      this.#route = parse(location.pathname);
    });
  }
}

export const router: { readonly route: Route; go(path: string): void } = new Router();
