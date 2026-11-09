<script>
  // Like a scanner: new calls from the chosen channels play one after another as they finish.
  import { onMount } from 'svelte';
  import { api, formatDuration, formatMHz, formatTime } from '../lib.js';
  import NowPlaying from '../NowPlaying.svelte';
  import { CallPlayer } from '../player.svelte.js';

  const STORE = 'sentrysdr.scanner.targets';
  const player = new CallPlayer();
  let targets = $state([]);
  let chosen = $state(load());
  let error = $state('');

  function load() {
    try {
      return JSON.parse(localStorage.getItem(STORE) ?? '[]').filter(Number.isInteger);
    } catch {
      return [];
    }
  }

  function remember() {
    try {
      localStorage.setItem(STORE, JSON.stringify(chosen));
    } catch {
      // A private window may refuse storage; the choice still works for now.
    }
  }

  const voice = $derived(targets.filter((t) => t.kind === 'audio'));

  async function poll() {
    if (!player.on || !chosen.length || document.hidden) return;
    try {
      const reply = await api(`recordings?targets=${chosen.join(',')}&ended_after=${player.since - 1}&limit=50`);
      player.offer(reply.items);
      error = '';
    } catch (failure) {
      error = failure.message;
    }
  }

  function toggle(id) {
    chosen = chosen.includes(id) ? chosen.filter((t) => t !== id) : [...chosen, id];
    remember();
  }

  function start() {
    player.start();
    poll();
  }

  onMount(() => {
    api('targets').then((list) => {
      targets = list;
      chosen = chosen.filter((id) => list.some((t) => t.id === id));  // forget deleted targets
    }).catch((failure) => (error = failure.message));
    const timer = setInterval(poll, 3000);
    return () => {
      clearInterval(timer);
      player.stop();
    };
  });
</script>

<h1>Scanner</h1>
<p class="small">New calls from the channels you choose play one after another as they finish. Hold stays on the channel that's playing; Skip moves on.</p>
{#if error}<div class="err" role="alert">{error}</div>{/if}

<NowPlaying {player} />

<fieldset>
  <legend>Channels</legend>
  {#each voice as target (target.id)}
    <label class="tick"><input type="checkbox" checked={chosen.includes(target.id)} onchange={() => toggle(target.id)} /> {target.name} <span class="mono small dim">{formatMHz(target.frequency_hz)}</span></label>
  {:else}
    <p class="small dim">No voice targets yet.</p>
  {/each}
  <div class="buttons">
    <button type="button" onclick={() => (chosen = voice.map((t) => t.id), remember())}>All</button>
    <button type="button" onclick={() => (chosen = [], remember())}>None</button>
  </div>
</fieldset>

<div class="buttons">
  {#if player.on}
    <button class="bad" onclick={() => player.stop()}>Stop scanning</button>
  {:else}
    <button class="main" disabled={!chosen.length} onclick={start}>Start scanning</button>
  {/if}
</div>

{#if player.played.length}
  <div class="box">
    <h2>Just played</h2>
    {#each player.played as call (call.id)}
      <div class="call">
        <div class="small"><strong>{call.target_name}</strong>, {formatTime(call.started_at)}, {formatDuration(call.duration)}</div>
        <!-- svelte-ignore a11y_media_has_caption -->
        <audio src={`api/recordings/${call.id}/audio`} controls preload="none"></audio>
      </div>
    {/each}
  </div>
{/if}
