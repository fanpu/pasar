import { getMascot } from "./api";
import type { MascotState } from "./mood";
import type { JobSpriteManifest, MascotManifest } from "./types";

const EMPTY_JOB_SPRITES: JobSpriteManifest = { local: {}, cloud: {} };

/** Which mascot image to show for a given state: a random pick from the user's `mascot_dir`
 * manifest, falling back to the built-in art when the user hasn't supplied one. */
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
    const urls = this.manifest[state] as string[] | undefined;
    if (!urls || urls.length === 0) return `/mascot/builtin/${state}.png`;
    return urls[Math.floor(rand() * urls.length)];
  }

  // The manifest's "jobs" key is a JobSpriteManifest, not a plain URL list like every other key
  // — see MascotManifest's own comment — so this is the one place that casts to it, with an
  // empty-both-kinds fallback before the manifest has loaded (or for a user with no job sprites
  // at all) so callers never need to null-check the kind/state lookups themselves.
  get jobSprites(): JobSpriteManifest {
    return (this.manifest.jobs as JobSpriteManifest | undefined) ?? EMPTY_JOB_SPRITES;
  }
}

export const mascot = new Mascot();
