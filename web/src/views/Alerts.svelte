<script>
  // Calls and messages that matched your keywords, or were marked important, since you last looked.
  import { onMount } from 'svelte';
  import { api, formatDuration, formatTime } from '../lib.js';

  let { onSeen } = $props();
  let data = $state(null);
  let error = $state('');

  async function load() {
    try {
      data = await api('alerts');
      error = '';
    } catch (failure) {
      error = failure.message;
    }
  }

  async function markSeen() {
    await api('alerts/seen', { method: 'POST' }).catch(() => {});
    await load();
    onSeen?.();
  }

  onMount(() => {
    load();
    const timer = setInterval(() => !document.hidden && load(), 15000);
    return () => clearInterval(timer);
  });
</script>

<h1>Alerts</h1>
<p class="small">Calls and ACARS messages that match your keywords, or that someone marked important{data ? `, since ${formatTime(data.since)}` : ''}. Set your keywords under <a href="#/account">My account</a>.</p>
{#if error}<div class="err" role="alert">{error}</div>{/if}

{#if data}
  <div class="buttons"><button onclick={markSeen} disabled={!data.count}>Mark all seen</button></div>
  <div class="box">
    {#each data.items as item (`${item.kind}-${item.id}`)}
      <div class="call important">
        <div>
          <strong>{item.title}</strong>
          <span class="small">{item.kind === 'call' ? `${formatTime(item.started_at)}, ${formatDuration(item.duration)}` : formatTime(item.at)}</span>
        </div>
        <div class="small">
          {#if item.matches.length}<span class="st-bad">Matches: {item.matches.join(', ')}</span>{/if}
          {#if item.important}{item.matches.length ? ' · ' : ''}<span class="st-bad">Marked important{item.important_by ? ` by ${item.important_by}` : ''}</span>{/if}
        </div>
        {#if item.kind === 'call'}
          <!-- svelte-ignore a11y_media_has_caption -->
          <audio src={`api/recordings/${item.id}/audio`} controls preload="none"></audio>
          {#if item.text}<div class="transcript">{item.text}</div>{/if}
        {:else}
          <pre class="message-text mono small">{item.text}</pre>
        {/if}
      </div>
    {:else}
      <p class="pad dim">Nothing new.</p>
    {/each}
  </div>
{/if}
