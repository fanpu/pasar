<script lang="ts">
  import { hm } from "../lib/format";
  import type { EventRow } from "../lib/eventlog";

  interface Props {
    rows: EventRow[];
  }
  let { rows }: Props = $props();
</script>

{#if rows.length === 0}
  <p class="dim">No events yet.</p>
{:else}
  <div class="events">
    {#each rows as r, i (i)}
      <div class="ev">
        <span class="t">{hm(r.ts)}</span>
        <span class="k" aria-hidden="true">{r.icon}</span>
        <span>{r.text}</span>
      </div>
    {/each}
  </div>
{/if}
