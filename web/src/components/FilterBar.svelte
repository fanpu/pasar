<script lang="ts">
  import { untrack } from "svelte";
  import { jobColor } from "../lib/colors";
  import { EMPTY_FILTER, STATE_FILTERS, isActive, type Filter, type StateFilter } from "../lib/jobfilter";

  interface Props {
    filter: Filter;
    counts: Record<StateFilter, number>;
    known: { tags: string[]; by: string[] };
    onchange: (f: Filter, replace?: boolean) => void;
  }
  let { filter, counts, known, onchange }: Props = $props();

  const SEARCH_DEBOUNCE_MS = 200;

  // A local draft so typing feels instant; the URL (and therefore `filter.q`) only updates once
  // the user pauses, via replaceState so it doesn't spam history.
  let qDraft = $state(untrack(() => filter.q));
  $effect(() => {
    qDraft = filter.q;
  });

  let debounceTimer: ReturnType<typeof setTimeout> | null = null;
  function onSearchInput(e: Event): void {
    qDraft = (e.target as HTMLInputElement).value;
    if (debounceTimer !== null) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      onchange({ ...filter, q: qDraft }, true);
    }, SEARCH_DEBOUNCE_MS);
  }

  function toggleState(s: StateFilter): void {
    const states = filter.states.includes(s) ? filter.states.filter((x) => x !== s) : [...filter.states, s];
    onchange({ ...filter, states }, false);
  }

  function removeTag(tag: string): void {
    onchange({ ...filter, tags: filter.tags.filter((t) => t !== tag) }, false);
  }
  function removeBy(by: string): void {
    onchange({ ...filter, by: filter.by.filter((b) => b !== by) }, false);
  }

  function onAddTag(e: Event): void {
    const select = e.target as HTMLSelectElement;
    const tag = select.value;
    select.value = "";
    if (tag && !filter.tags.includes(tag)) onchange({ ...filter, tags: [...filter.tags, tag] }, false);
  }
  function onAddBy(e: Event): void {
    const select = e.target as HTMLSelectElement;
    const by = select.value;
    select.value = "";
    if (by && !filter.by.includes(by)) onchange({ ...filter, by: [...filter.by, by] }, false);
  }

  function clear(): void {
    onchange(EMPTY_FILTER, false);
  }

  const addableTags = $derived(known.tags.filter((t) => !filter.tags.includes(t)));
  const addableBy = $derived(known.by.filter((b) => !filter.by.includes(b)));
</script>

<div class="fbar">
  <div class="search">
    🔍
    <input
      type="search"
      placeholder="search name, command, note, #id…"
      aria-label="search jobs"
      value={qDraft}
      oninput={onSearchInput}
    />
  </div>
  <div class="states">
    {#each STATE_FILTERS as s (s)}
      <button
        type="button"
        class="st s-{s}"
        class:off={!filter.states.includes(s) && counts[s] === 0}
        aria-pressed={filter.states.includes(s)}
        onclick={() => toggleState(s)}
      >
        {s} {counts[s]}
      </button>
    {/each}
  </div>
  <div class="facets">
    {#each filter.tags as tag (tag)}
      {@const c = jobColor({ id: 0, tags: [tag] })}
      <span class="fchip" style="background: {c}1f; color: {c}">
        tag: {tag}
        <button type="button" class="xbtn" aria-label="remove tag {tag}" onclick={() => removeTag(tag)}>×</button>
      </span>
    {/each}
    {#each filter.by as by (by)}
      <span class="fchip">
        by: {by}
        <button type="button" class="xbtn" aria-label="remove submitter {by}" onclick={() => removeBy(by)}>×</button>
      </span>
    {/each}
    <select class="add" aria-label="add tag filter" onchange={onAddTag}>
      <option value="">+ tag</option>
      {#each addableTags as t (t)}<option value={t}>{t}</option>{/each}
    </select>
    <select class="add" aria-label="add submitter filter" onchange={onAddBy}>
      <option value="">+ submitter</option>
      {#each addableBy as b (b)}<option value={b}>{b}</option>{/each}
    </select>
    {#if isActive(filter)}
      <button type="button" class="clear" onclick={clear}>clear</button>
    {/if}
  </div>
</div>

<style>
  .fbar { display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; margin: 4px 0 12px; }
  .search {
    flex: 1 1 260px; display: flex; align-items: center; gap: 6px; border: 1.5px solid var(--line-2);
    border-radius: 99px; padding: 7px 14px; background: #fffdfb;
  }
  .search input {
    flex: 1; border: 0; background: transparent; outline: none; font: inherit; font-weight: 700; color: var(--ink);
  }
  .search input::placeholder { color: var(--ink-3); }

  .states { display: flex; gap: 6px; flex-wrap: wrap; }
  .st { padding: 4px 10px; border-radius: 99px; font-size: 12px; font-weight: 800; border: 0; }
  .st.off { background: transparent; border: 1.5px dashed var(--line-2); color: var(--ink-3); }

  .facets { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .fchip {
    display: inline-flex; align-items: center; gap: 4px; padding: 4px 6px 4px 10px; border-radius: 99px;
    font-size: 12px; font-weight: 800; background: var(--line); color: var(--ink-2);
  }
  .xbtn { background: none; border: 0; padding: 0 2px; font: inherit; font-weight: 900; color: inherit; cursor: pointer; }
  .add {
    padding: 3px 10px; border-radius: 99px; border: 1.5px dashed var(--line-2); font-size: 12px; font-weight: 800;
    color: var(--ink-2); background: var(--card);
  }
  .clear { background: none; border: 0; font: inherit; font-size: 12px; font-weight: 800; color: var(--accent-ink); margin-left: 4px; cursor: pointer; }
</style>
