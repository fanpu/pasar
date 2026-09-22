<script lang="ts">
  import type { Mood } from "../lib/mood";

  interface Props {
    banner: Mood["banner"];
    connected: boolean;
    // True once the stream has connected at least once (or a snapshot exists). Before that, a
    // disconnected `connected` prop just means "hasn't finished the first connection attempt
    // yet" — not a real loss of connection — so the banner stays quiet until then.
    hasConnectedOnce: boolean;
  }
  let { banner, connected, hasConnectedOnce }: Props = $props();

  const shown = $derived(
    connected
      ? banner
      : hasConnectedOnce
        ? { tone: "warn" as const, text: "Lost connection to pasard. Retrying…" }
        : null,
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
