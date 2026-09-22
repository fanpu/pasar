import { getMascot } from "./api";
import type { MascotState } from "./mood";
import type { MascotManifest } from "./types";

/** Which mascot image to show for a given state: a random pick from the user's `mascot_dir`
 * manifest, falling back to the repo's built-in SVG when the user hasn't supplied one. */
export class Mascot {
  manifest = $state.raw<MascotManifest>({});
  // Flips once load() resolves. A primitive (rather than the manifest object itself) so callers
  // that re-pick a mascot image keyed on "has the manifest arrived yet" don't need to compare
  // object identity — before this flips, pick() only ever returns built-in art.
  loaded = $state(false);

  async load(fetcher: () => Promise<MascotManifest> = getMascot): Promise<void> {
    this.manifest = await fetcher();
    this.loaded = true;
  }

  pick(state: MascotState, rand: () => number = Math.random): string {
    const urls = this.manifest[state];
    if (!urls || urls.length === 0) return `/mascot/builtin/${state}.svg`;
    return urls[Math.floor(rand() * urls.length)];
  }
}

export const mascot = new Mascot();
