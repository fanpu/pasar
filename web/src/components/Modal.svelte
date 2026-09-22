<script lang="ts">
  import type { Snippet } from "svelte";

  interface Props {
    title: string;
    onclose: () => void;
    children: Snippet;
    // A form with several fields is cramped at the default width; opt into a wider modal.
    wide?: boolean;
  }
  let { title, onclose, children, wide = false }: Props = $props();

  function onkeydown(e: KeyboardEvent): void {
    if (e.key === "Escape") onclose();
  }
</script>

<svelte:window onkeydown={onkeydown} />
<button type="button" class="mscrim" aria-label="Close" onclick={onclose}></button>
<div class="modal" class:wide role="dialog" aria-modal="true" aria-label={title}>
  <div class="mhead">
    <h3>{title}</h3>
    <button class="x" type="button" onclick={onclose} aria-label="Close">✕</button>
  </div>
  {@render children()}
</div>

<style>
  .mscrim { display: block; position: fixed; inset: 0; margin: 0; padding: 0; border: 0; background: rgba(74, 63, 77, .18); z-index: 40; cursor: default; }
  .modal {
    position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
    width: min(360px, 92vw); background: var(--card); border: 1.5px solid var(--line);
    border-radius: var(--r); box-shadow: var(--shadow); padding: 16px 18px; z-index: 41;
  }
  .modal.wide { width: min(560px, 100vw - 24px); }
  .mhead { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
  .mhead h3 { margin: 0; font-size: 16px; font-weight: 900; }
</style>
