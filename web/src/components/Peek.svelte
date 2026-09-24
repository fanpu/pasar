<script lang="ts">
  interface Props {
    src: string | null;
  }
  let { src }: Props = $props();
</script>

{#if src}
  <img class="peek" {src} alt="" aria-hidden="true" />
{/if}

<style>
  /* Fixed at the bottom-left corner, mostly off-screen as if peeking in. `pointer-events: none`
     so it never intercepts a click, and a z-index below both the modal scrim (40) and the toast
     (60) so either one covers it if they ever overlap. The slide-in only plays once, on mount —
     `prefers-reduced-motion` already turns off every animation site-wide (see app.css), which
     leaves the image simply in its resting place instead of the pre-animation position. */
  .peek {
    position: fixed; left: -30px; bottom: -30px; width: 140px; height: auto;
    pointer-events: none; z-index: 10; user-select: none;
    animation: peek-in .5s cubic-bezier(.2, 1.4, .4, 1);
  }

  @keyframes peek-in {
    from { transform: translate(-40%, 40%); opacity: 0; }
    to { transform: translate(0, 0); opacity: 1; }
  }

  @media (max-width: 760px) {
    /* Smaller and tucked further into the corner than on desktop: phone viewports are short
       enough that a card's heading can end up right at the bottom edge, and this keeps the
       visible sliver clear of it (checked against the fake-cloud e2e page). */
    .peek { width: 76px; left: -22px; bottom: -22px; }
  }
</style>
