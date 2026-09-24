<script lang="ts">
  import { reasonLabel } from "../lib/format";
  import type { JobView } from "../lib/types";

  interface Props {
    job: JobView;
  }
  let { job }: Props = $props();

  const ICONS: Record<JobView["state"], string> = {
    running: "●", queued: "◷", stopping: "◐", completed: "✓", failed: "✕", cancelled: "–",
    awaiting: "◔",
  };

  const text = $derived.by(() => {
    switch (job.state) {
      case "running":
        return "running";
      case "stopping":
        if (job.stop_requested === "preempt") return "preempting";
        if (job.stop_requested === "cancel") return "cancelling";
        return "stopping";
      case "queued":
        return job.preemptions > 0 ? `queued · preempted ×${job.preemptions}` : "queued";
      case "completed":
        return "completed";
      case "failed":
        return reasonLabel(job.reason) || "failed";
      case "cancelled":
        return "cancelled";
      case "awaiting":
        return "awaiting your OK";
      default:
        return job.state;
    }
  });
</script>

<span class="pill s-{job.state}"><span class="ic" aria-hidden="true">{ICONS[job.state]}</span>{text}</span>
