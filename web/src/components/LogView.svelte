<script lang="ts">
  import { ApiError, followLog, getLog } from "../lib/api";

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

  const RETRY_MS = 3000;

  function errorMessage(e: unknown): string {
    return e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
  }

  // Full backlog load: reset on every id change and read in ≤1 MiB chunks until an empty one.
  // A transient getLog failure doesn't drop what's already loaded — it shows an inline error and
  // retries after a few seconds, resuming from the last successfully read offset rather than
  // re-appending text already shown.
  let endOffset = $state<number | null>(null);
  let loadError = $state<string | null>(null);
  let loadGen = 0;

  $effect(() => {
    const forId = id;
    const myGen = ++loadGen;
    buffer = "";
    lines = [];
    endOffset = null;
    loadError = null;

    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    async function loadFrom(offset: number): Promise<void> {
      while (!cancelled) {
        let chunk: { text: string; offset: number };
        try {
          chunk = await getLog(forId, offset);
        } catch (e) {
          if (cancelled || myGen !== loadGen) return;
          loadError = errorMessage(e);
          retryTimer = setTimeout(() => {
            retryTimer = null;
            if (!cancelled && myGen === loadGen) void loadFrom(offset);
          }, RETRY_MS);
          return;
        }
        if (cancelled || myGen !== loadGen) return;
        loadError = null;
        offset = chunk.offset;
        if (chunk.text === "") break;
        appendText(chunk.text);
      }
      if (cancelled || myGen !== loadGen) return;
      endOffset = offset;
    }
    void loadFrom(0);

    return () => {
      cancelled = true;
      if (retryTimer !== null) clearTimeout(retryTimer);
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
  <div class="log" style="height: {height}px">
    {loadError ? `Couldn't load the log: ${loadError}` : "No output yet."}
  </div>
{:else}
  <div class="logwrap">
    <div class="log" style="height: {height}px" role="log" bind:this={containerEl} onscroll={onscroll}>
      {#each lines as line, i (i)}
        <div class="ln {line.cls}">{line.text}</div>
      {/each}
    </div>
    {#if loadError}<div class="logerr small">Couldn't load the log: {loadError}</div>{/if}
    {#if !stickToBottom}
      <button type="button" class="jumpend" onclick={jumpToEnd}>jump to end</button>
    {/if}
  </div>
{/if}

<style>
  .logwrap { position: relative; }
  .logerr { color: var(--fail); font-weight: 800; margin-top: 6px; }
  .jumpend {
    position: absolute; bottom: 10px; left: 50%; transform: translateX(-50%);
    background: var(--accent); color: #fff; border: 0; border-radius: 99px;
    padding: 5px 12px; font-size: 12px; font-weight: 800; box-shadow: var(--shadow);
  }
</style>
