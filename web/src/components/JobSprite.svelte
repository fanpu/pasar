<script lang="ts">
  import { spriteLabel, spriteUrl } from "../lib/jobsprite";
  import { mascot } from "../lib/mascot.svelte";
  import type { JobView } from "../lib/types";

  interface Props {
    job: JobView;
  }
  let { job }: Props = $props();

  const url = $derived(spriteUrl(mascot.jobSprites, job));
  const label = $derived(spriteLabel(job));
</script>

{#if url}
  <span class="jobsprite" role="img" aria-label={label} title={label}>
    <!-- No `loading="lazy"`: JobTable renders every row twice (the desktop `<table>` and the
         mobile `.cards` list, one hidden by CSS depending on viewport width), so each sprite URL
         appears in two `<img>`s at once. Chromium's native lazy-loading, keyed on the same URL,
         never resolved the hidden copy's intersection and — for reasons not worth chasing
         further — left the visible one unloaded too; eager loading (still `decoding="async"`,
         so it doesn't block paint) sidesteps that entirely, and these are ~40px icons, not the
         heavy below-the-fold media lazy-loading is for. -->
    <img src={url} alt="" aria-hidden="true" decoding="async" />
  </span>
{/if}

<style>
  /* A fixed box so a job with no sprite (the common case until someone drops files in) leaves
     the row exactly as before, and one that has a sprite never shifts the row while it loads. */
  .jobsprite { display: inline-flex; flex: none; width: 40px; height: 40px; pointer-events: none; }
  .jobsprite img { width: 100%; height: 100%; object-fit: contain; }

  @media (max-width: 760px) {
    .jobsprite { width: 32px; height: 32px; }
  }
</style>
