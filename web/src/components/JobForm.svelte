<script lang="ts">
  import { untrack } from "svelte";
  import type { JobDetail, JobView, RestartBody, SubmitBody } from "../lib/types";
  import { submitJob, restartJob, ApiError } from "../lib/api";
  import { gib } from "../lib/format";
  import Modal from "./Modal.svelte";

  interface Props {
    mode: "submit" | "restart";
    job?: JobDetail;
    onclose: () => void;
    ondone: (job: JobView) => void;
  }
  let { mode, job, onclose, ondone }: Props = $props();

  const CWD_KEY = "pasar.cwd";

  // Restart-mode prefills, captured once (via untrack — these are one-shot form defaults, not
  // meant to track the live prop) so later edits can be compared back against them to build a
  // "changed fields only" RestartBody.
  const initialMode: "whole" | "shared" = untrack(() => job?.mode ?? "whole");
  const initialSize = untrack(() =>
    job && job.mode === "shared" && job.mem_request !== null ? `${gib(job.mem_request)}G` : "",
  );
  const initialTime = untrack(() => (job ? `${Math.round(job.est_runtime / 60)}m` : ""));
  const initialBid = untrack(() => job?.bid ?? 1000);
  const initialRetries = untrack(() => job?.retries ?? 0);
  const isSubmit = untrack(() => mode === "submit");
  const title = untrack(() =>
    mode === "submit" ? "Submit a job" : `Restart #${job?.id ?? ""} with changes`,
  );

  let command = $state("");
  let cwd = $state(isSubmit ? (localStorage.getItem(CWD_KEY) ?? "") : "");
  let memMode = $state<"whole" | "shared">(initialMode);
  let size = $state(initialSize);
  let time = $state(initialTime);
  let bid = $state(initialBid);
  let name = $state("");
  let note = $state("");
  let preemptible = $state(true);
  let retries = $state(initialRetries);

  let error = $state<string | null>(null);
  let saving = $state(false);
  let formEl: HTMLFormElement | undefined;

  function buildRestartBody(current: JobDetail): RestartBody {
    const body: RestartBody = {};
    if (time !== initialTime) body.time = time;
    if (bid !== current.bid) body.bid = bid;
    if (retries !== current.retries) body.retries = retries;
    if (memMode === "whole" && initialMode === "shared") {
      body.whole_gpu = true;
    } else if (memMode === "shared" && (initialMode === "whole" || size !== initialSize)) {
      body.mem = size;
    }
    return body;
  }

  async function handleSubmit(): Promise<void> {
    if (saving) return;
    error = null;
    if (mode === "submit") {
      if (!command.trim() || !cwd.trim() || !time.trim()) {
        error = "command, directory and time are required";
        return;
      }
    } else if (!time.trim()) {
      error = "time is required";
      return;
    }
    if (memMode === "shared" && !size.trim()) {
      error = "memory size is required for shared jobs";
      return;
    }
    saving = true;
    try {
      let result: JobView;
      if (mode === "submit") {
        const body: SubmitBody = {
          command, cwd, time, mem: memMode === "shared" ? size : null,
          bid, preemptible, retries, name, note, submitter: "web",
        };
        result = await submitJob(body);
        try {
          localStorage.setItem(CWD_KEY, cwd);
        } catch {
          // Best-effort: a storage failure (quota, private browsing) shouldn't surface as a
          // submit error or block ondone()/onclose() below, which would invite a duplicate submit.
        }
      } else {
        const current = job;
        if (!current) return;
        result = await restartJob(current.id, buildRestartBody(current));
      }
      ondone(result);
      onclose();
    } catch (e) {
      error = e instanceof ApiError ? e.message : String(e);
    } finally {
      saving = false;
    }
  }

  function onFormSubmit(e: SubmitEvent): void {
    e.preventDefault();
    void handleSubmit();
  }

  function onkeydown(e: KeyboardEvent): void {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (saving) return;
      // Go through the form's own submit event (rather than calling handleSubmit directly) so
      // this path is identical to clicking the submit button.
      formEl?.requestSubmit();
    }
  }
</script>

<svelte:window onkeydown={onkeydown} />
<Modal {title} {onclose} wide>
  <form bind:this={formEl} onsubmit={onFormSubmit}>
    {#if mode === "restart" && job}
      <div class="field">
        <span class="lbl">command</span>
        <div class="mono readonly">{job.command}</div>
      </div>
      <div class="field">
        <span class="lbl">directory</span>
        <div class="readonly">{job.cwd}</div>
      </div>
    {:else}
      <div class="field">
        <label for="jf-command">command</label>
        <textarea
          id="jf-command"
          class="mono"
          bind:value={command}
          placeholder="python train.py --config run.yaml"
        ></textarea>
      </div>
      <div class="field">
        <label for="jf-cwd">directory</label>
        <input id="jf-cwd" type="text" bind:value={cwd} placeholder="/home/you/project" />
      </div>
    {/if}

    <div class="field">
      <label for="jf-time">time</label>
      <input id="jf-time" type="text" bind:value={time} placeholder="2h" />
      <p class="hint">estimate; jobs aren't killed for running over</p>
    </div>

    <div class="field">
      <span class="lbl">memory</span>
      <div class="radios">
        <label><input type="radio" name="jf-mem-mode" value="whole" bind:group={memMode} /> whole GPU</label>
        <label><input type="radio" name="jf-mem-mode" value="shared" bind:group={memMode} /> shared</label>
      </div>
      {#if memMode === "shared"}
        <label for="jf-mem-size">memory size</label>
        <input id="jf-mem-size" type="text" bind:value={size} placeholder="24G" />
      {/if}
    </div>

    <div class="field">
      <label for="jf-bid">bid</label>
      <input id="jf-bid" type="number" min="0" step="1" bind:value={bid} />
      <p class="hint">1000 is normal. Higher bids can preempt lower ones.</p>
    </div>

    <div class="field">
      <label for="jf-retries">retries</label>
      <input id="jf-retries" type="number" min="0" step="1" bind:value={retries} />
    </div>

    {#if mode === "submit"}
      <div class="field">
        <label for="jf-name">name</label>
        <input id="jf-name" type="text" bind:value={name} />
      </div>
      <div class="field">
        <label for="jf-note">note</label>
        <input id="jf-note" type="text" bind:value={note} />
      </div>
      <div class="field">
        <label class="check"><input type="checkbox" bind:checked={preemptible} /> preemptible</label>
      </div>
    {/if}

    {#if error}<p class="err">{error}</p>{/if}

    <div class="acts">
      <button class="btn primary" type="submit" disabled={saving}>
        {mode === "submit" ? "Submit" : "Restart"}
      </button>
      <button class="btn" type="button" disabled={saving} onclick={onclose}>Cancel</button>
    </div>
  </form>
</Modal>

<style>
  form { max-height: 72vh; overflow-y: auto; padding-right: 2px; }
  .field { margin-bottom: 10px; }
  .field label, .field .lbl {
    display: block; font-size: 12px; font-weight: 800; color: var(--ink-2); margin-bottom: 4px;
  }
  input[type="text"], input[type="number"], textarea {
    width: 100%; font: inherit; font-weight: 700; padding: 8px 10px; border-radius: 12px;
    border: 1.5px solid var(--line-2); color: var(--ink); background: var(--card);
  }
  textarea { min-height: 64px; resize: vertical; }
  .readonly {
    padding: 8px 10px; border-radius: 12px; border: 1.5px solid var(--line);
    background: #faf6f8; color: var(--ink-2); word-break: break-word;
  }
  .hint { font-size: 12px; color: var(--ink-2); font-weight: 700; margin: 4px 0 0; }
  .radios { display: flex; gap: 14px; margin-bottom: 6px; }
  .radios label {
    display: flex; align-items: center; gap: 6px; font-weight: 700; color: var(--ink);
    margin-bottom: 0;
  }
  .radios input { width: auto; }
  .check { display: flex !important; flex-direction: row; align-items: center; gap: 8px; font-weight: 700; color: var(--ink); }
  .check input { width: auto; }
  .err { color: var(--fail); font-weight: 800; font-size: 12.5px; margin: 0 0 8px; }
  .acts { display: flex; gap: 8px; margin-top: 4px; }
</style>
