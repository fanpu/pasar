<script lang="ts">
  interface Props {
    message: string | null;
    image: string;
  }
  let { message, image }: Props = $props();
</script>

<div class="toast" class:show={message !== null} role="status" aria-live="polite">
  {#if message !== null}
    <img src={image} alt="" />
    <span>{message}</span>
  {/if}
</div>

<style>
  .toast {
    position: fixed; left: 18px; bottom: 18px; opacity: 0; visibility: hidden; transform: translateY(40px);
    transition: transform .35s cubic-bezier(.2, 1.4, .4, 1), opacity .2s, visibility .35s;
    background: var(--card); border: 1.5px solid var(--line); border-radius: 99px; padding: 4px 18px 4px 4px;
    display: flex; align-items: center; gap: 8px; font-weight: 800; box-shadow: 0 8px 24px rgba(214, 170, 190, .35);
    z-index: 60;
  }
  .toast.show { transform: none; opacity: 1; visibility: visible; }
  .toast img { width: 46px; height: 46px; }

  @media (max-width: 760px) {
    .toast { left: 8px; right: 8px; bottom: 96px; }
  }
</style>
