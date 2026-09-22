<script lang="ts">
  import type { Mood } from "../lib/mood";

  interface Props {
    mood: Mood;
    image: string;
    bounceKey: number;
    onsubmit: () => void;
  }
  let { mood, image, bounceKey, onsubmit }: Props = $props();
</script>

<header>
  {#key bounceKey}
    <img class="mascot" src={image} alt="" width="118" height="118" />
  {/key}
  <div class="bubble">{mood.say}<small>{mood.sub}</small></div>
  <div class="spacer"></div>
  <span class="brand">pasar</span>
  <button class="submit" onclick={onsubmit}>+ submit</button>
</header>

<style>
  header { display: flex; align-items: center; gap: 14px; }
  .brand { font-weight: 900; font-size: 20px; color: var(--accent-ink); letter-spacing: .3px; margin-right: 6px; }
  .mascot { width: 118px; height: 118px; flex: none; animation: bounce .6s ease; }
  @keyframes bounce {
    0% { transform: translateY(0); }
    30% { transform: translateY(-12px); }
    60% { transform: translateY(0); }
    80% { transform: translateY(-4px); }
    100% { transform: translateY(0); }
  }
  .bubble {
    position: relative; background: var(--card); border: 1.5px solid var(--line); border-radius: 20px;
    padding: 12px 18px; font-weight: 800; font-size: 15px; box-shadow: var(--shadow); max-width: 560px;
  }
  .bubble:before {
    content: ""; position: absolute; left: -9px; top: 50%; margin-top: -8px;
    border: 8px solid transparent; border-left: 0; border-right-color: var(--line);
  }
  .bubble:after {
    content: ""; position: absolute; left: -6.5px; top: 50%; margin-top: -6.5px;
    border: 6.5px solid transparent; border-left: 0; border-right-color: var(--card);
  }
  .bubble small { display: block; font-weight: 700; font-size: 12.5px; color: var(--ink-2); margin-top: 2px; }
  .submit {
    background: var(--accent); color: #fff; border: 0; border-radius: 99px; padding: 9px 16px;
    font-weight: 900; box-shadow: 0 4px 12px rgba(255, 143, 171, .4);
  }

  @media (max-width: 760px) {
    .mascot { width: 84px; height: 84px; }
    .bubble { font-size: 13.5px; padding: 10px 13px; }
    .brand, .submit { display: none; }
  }
</style>
