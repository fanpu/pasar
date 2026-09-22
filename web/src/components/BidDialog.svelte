<script lang="ts">
  import { untrack } from "svelte";
  import Modal from "./Modal.svelte";

  interface Props {
    bid: number;
    onsave: (bid: number) => Promise<void>;
    onclose: () => void;
  }
  let { bid, onsave, onclose }: Props = $props();

  // Seed the editable field from the bid prop once; it's a local draft, not synced afterward.
  let value = $state(untrack(() => bid));
  let saving = $state(false);
  let error = $state<string | null>(null);

  const QUICK_BIDS = [800, 1000, 1500, 2000];

  async function save(): Promise<void> {
    saving = true;
    error = null;
    try {
      await onsave(value);
      onclose();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      saving = false;
    }
  }

  function onkeydown(e: KeyboardEvent): void {
    if (e.key === "Enter") {
      e.preventDefault();
      void save();
    }
  }
</script>

<Modal title="Set bid" {onclose}>
  <input type="number" min="0" step="1" bind:value aria-label="bid" onkeydown={onkeydown} />
  <div class="quick">
    {#each QUICK_BIDS as q (q)}
      <button type="button" class="btn" onclick={() => (value = q)}>{q}</button>
    {/each}
  </div>
  <p class="hint">1000 is normal. Higher bids can preempt lower ones.</p>
  {#if error}<p class="err">{error}</p>{/if}
  <div class="acts">
    <button class="btn primary" type="button" disabled={saving} onclick={save}>Save</button>
    <button class="btn" type="button" disabled={saving} onclick={onclose}>Cancel</button>
  </div>
</Modal>

<style>
  input[type="number"] {
    width: 100%; font: inherit; font-weight: 800; padding: 8px 10px; border-radius: 12px;
    border: 1.5px solid var(--line-2); margin-bottom: 8px; color: var(--ink);
  }
  .quick { display: flex; gap: 6px; margin-bottom: 8px; }
  .hint { font-size: 12px; color: var(--ink-2); font-weight: 700; margin: 0 0 10px; }
  .err { color: var(--fail); font-weight: 800; font-size: 12.5px; margin: 0 0 8px; }
</style>
