<script lang="ts">
  import type { Mood } from "../lib/mood";

  interface Props {
    banner: Mood["banner"];
    connected: boolean;
  }
  let { banner, connected }: Props = $props();

  const shown = $derived(
    connected ? banner : { tone: "warn" as const, text: "Lost connection to pasard. Retrying…" },
  );
</script>

{#if shown}
  <div class="banner show {shown.tone}">{shown.text}</div>
{/if}

<style>
  .banner { margin: 12px 0 0; border-radius: 16px; padding: 10px 14px; font-weight: 800; display: flex; gap: 8px; align-items: center; }
  .banner.warn { background: var(--stop-bg); color: #8a5a12; }
  .banner.bad { background: var(--fail-bg); color: #a3364a; }
</style>
