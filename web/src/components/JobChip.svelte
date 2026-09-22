<script lang="ts">
  import { jobColor } from "../lib/colors";

  interface Props {
    id: number;
  }
  let { id }: Props = $props();

  const color = $derived(jobColor(id));
</script>

<div class="chip" style="background: {color}22; color: {color}; box-shadow: inset 0 0 0 2px {color}">{id}</div>
