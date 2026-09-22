<script lang="ts">
  import { ago, dur, fmtGib, hm } from "../lib/format";
  import type { JobDetail, JobView } from "../lib/types";

  interface Props {
    job: JobView;
    detail: JobDetail | null;
    now: number;
  }
  let { job, now }: Props = $props();

  interface Fact {
    l: string;
    v: string;
    s: string;
  }

  function progressFact(p: JobView["progress"]): [string, string] {
    if (p === null) return ["–", "no progress reported"];
    if (p.total_steps === null) return [`step ${p.step}`, ""];
    const pct = Math.round(((p.step ?? 0) / p.total_steps) * 100);
    return [`${pct}%`, `step ${p.step} / ${p.total_steps}`];
  }

  function checkpointFact(c: JobView["last_checkpoint"]): [string, string] {
    if (c === null) return ["none yet", ""];
    return [ago(c.ts, now), c.step !== null ? `step ${c.step}` : ""];
  }

  function plural(n: number, word: string): string {
    return `${n} ${word}${n > 1 ? "s" : ""}`;
  }

  function lostFact(j: JobView): [string, string] {
    const total = j.lost.preemption + j.lost.failure;
    const value = total > 0 ? dur(total) : "none";
    const sub = j.preemptions > 0 ? plural(j.preemptions, "preemption") : "";
    return [value, sub];
  }

  function attemptFact(j: JobView): [string, string] {
    if (j.attempts <= 1) return [`${j.attempts}`, "first run"];
    const k = j.attempts - 1;
    return [`${j.attempts}`, `resumed after ${plural(k, "preemption")}`];
  }

  function bidSub(bid: number): string {
    if (bid < 1000) return "below default";
    if (bid === 1000) return "default";
    return "above default";
  }

  const facts = $derived.by((): Fact[] => {
    if (job.state === "running" || job.state === "stopping") {
      const [pv, ps] = progressFact(job.progress);
      const [cv, cs] = checkpointFact(job.last_checkpoint);
      const [lv, ls] = lostFact(job);
      const [av, as] = attemptFact(job);
      return [
        { l: "elapsed", v: dur(job.run_time), s: `of ~${dur(job.est_runtime)} estimated` },
        { l: "progress", v: pv, s: ps },
        { l: "last checkpoint", v: cv, s: cs },
        { l: "memory", v: fmtGib(job.usage), s: `limit ${fmtGib(job.limit)} · peak ${fmtGib(job.peak)}` },
        { l: "lost time", v: lv, s: ls },
        { l: "attempt", v: av, s: as },
      ];
    }
    if (job.state === "queued") {
      const [lv, ls] = lostFact(job);
      const starts = job.projected.length > 0 ? `~${hm(job.projected[0][0])}` : "–";
      const startsSub = job.projected.length > 0 ? "projected" : "not scheduled yet";
      const needsV = job.mode === "whole" ? "whole GPU" : fmtGib(job.mem_request);
      const needsS = job.mode === "whole" ? "" : `reserves ${fmtGib(job.limit)}`;
      return [
        { l: "starts", v: starts, s: startsSub },
        { l: "estimate", v: dur(job.est_runtime), s: job.run_time > 0 ? `${dur(job.run_time)} already run` : "not started" },
        { l: "needs", v: needsV, s: needsS },
        { l: "bid", v: `★ ${job.bid}`, s: bidSub(job.bid) },
        { l: "lost time", v: lv, s: ls },
        { l: "by", v: job.submitter || "–", s: "" },
      ];
    }
    // finished: completed, failed, cancelled
    const firstStart = job.spans[0]?.[0] ?? job.start_time ?? job.submit_time;
    const total = job.lost.preemption + job.lost.failure;
    return [
      { l: "ran", v: dur(job.run_time), s: `${hm(firstStart)}–${hm(job.end_time ?? now)}` },
      { l: "peak memory", v: fmtGib(job.peak), s: `of ${fmtGib(job.limit)}` },
      { l: "lost time", v: total > 0 ? dur(total) : "none", s: job.preemptions > 0 ? plural(job.preemptions, "preemption") : "" },
    ];
  });
</script>

<div class="facts">
  {#each facts as f (f.l)}
    <div class="fact">
      <div class="l">{f.l}</div>
      <div class="v">{f.v}</div>
      <div class="s">{f.s}</div>
    </div>
  {/each}
</div>
