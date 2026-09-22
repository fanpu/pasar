<script lang="ts">
  import { followLog, getLog } from "../lib/api";

  interface Props {
    id: number;
    follow: boolean;
    height?: number;
  }
  let { id, follow, height = 210 }: Props = $props();

  const MAX_LINES = 5000;
  const NEAR_BOTTOM_PX = 40;

  interface Line {
    text: string;
    cls: string;
  }

  function classify(line: string): string {
    if (line.startsWith("────")) return "sep";
    if (/Error|Traceback|Exception/.test(line)) return "err";
    return "";
  }

  // Splits the raw buffer into complete lines. A trailing "\n" produces an empty final element
  // from String.split — that's "nothing written after the last newline yet", not a blank line,
  // so it's dropped. Without a trailing "\n" the final element is a genuine in-progress line.
  function splitLines(text: string): string[] {
    if (text === "") return [];
    const parts = text.split("\n");
    if (parts[parts.length - 1] === "") parts.pop();
    return parts;
  }

  let buffer = "";
  let lines = $state<Line[]>([]);

  function recompute(): void {
    let arr = splitLines(buffer);
    if (arr.length > MAX_LINES) {
      arr = arr.slice(-MAX_LINES);
      buffer = arr.join("\n") + (buffer.endsWith("\n") ? "\n" : "");
    }
    lines = arr.map((text) => ({ text, cls: classify(text) }));
  }

  function appendText(text: string): void {
    if (text === "") return;
    buffer += text;
    recompute();
  }

  // Full backlog load: reset on every id change and read in ≤1 MiB chunks until an empty one.
  let endOffset = $state<number | null>(null);
  let loadGen = 0;

  $effect(() => {
    const forId = id;
    const myGen = ++loadGen;
    buffer = "";
    lines = [];
    endOffset = null;

    let cancelled = false;
    (async () => {
      let offset = 0;
      while (!cancelled) {
        const chunk = await getLog(forId, offset);
        if (cancelled || myGen !== loadGen) return;
        offset = chunk.offset;
        if (chunk.text === "") break;
        appendText(chunk.text);
      }
      if (cancelled || myGen !== loadGen) return;
      endOffset = offset;
    })();

    return () => {
      cancelled = true;
    };
  });

  // Tailing: opens once the backlog finishes loading, closes on id change, `follow` toggling
  // off, or destroy.
  $effect(() => {
    if (!follow || endOffset === null) return;
    const stop = followLog(id, endOffset, (text) => appendText(text), () => {});
    return () => stop();
  });

  let containerEl = $state<HTMLDivElement | null>(null);
  let stickToBottom = $state(true);

  function onscroll(): void {
    if (!containerEl) return;
    const distance = containerEl.scrollHeight - containerEl.scrollTop - containerEl.clientHeight;
    stickToBottom = distance <= NEAR_BOTTOM_PX;
  }

  function jumpToEnd(): void {
    if (!containerEl) return;
    containerEl.scrollTop = containerEl.scrollHeight;
    stickToBottom = true;
  }

  $effect(() => {
    void lines;
    if (containerEl && stickToBottom) containerEl.scrollTop = containerEl.scrollHeight;
  });
</script>

{#if lines.length === 0}
  <div class="log" style="height: {height}px">No output yet.</div>
{:else}
  <div class="logwrap">
    <div class="log" style="height: {height}px" role="log" bind:this={containerEl} onscroll={onscroll}>
      {#each lines as line, i (i)}
        <div class="ln {line.cls}">{line.text}</div>
      {/each}
    </div>
    {#if !stickToBottom}
      <button type="button" class="jumpend" onclick={jumpToEnd}>jump to end</button>
    {/if}
  </div>
{/if}

<style>
  .logwrap { position: relative; }
  .jumpend {
    position: absolute; bottom: 10px; left: 50%; transform: translateX(-50%);
    background: var(--accent); color: #fff; border: 0; border-radius: 99px;
    padding: 5px 12px; font-size: 12px; font-weight: 800; box-shadow: var(--shadow);
  }
</style>
