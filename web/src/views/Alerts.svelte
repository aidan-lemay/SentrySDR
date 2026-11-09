<script>
  // Calls and messages that matched your keywords, or were marked important, since you last looked.
  import { onMount, untrack } from 'svelte';
  import { api, formatDuration, formatTime } from '../lib.js';
  import AlertSettings from './AlertSettings.svelte';
  import Emergencies from './Emergencies.svelte';

  let { route = '', user, onSeen } = $props();
  const tab = $derived(new URLSearchParams(route.split('?')[1] ?? '').get('tab') ?? '');
  const settingsTab = $derived(tab === 'settings');
  const canMaps = $derived(user?.permissions.includes('maps'));
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

  // Back on New alerts after changing your choices: show what they choose now.
  $effect(() => {
    if (tab === '') untrack(load);
  });

  onMount(() => {
    const timer = setInterval(() => !document.hidden && load(), 15000);
    return () => clearInterval(timer);
  });
</script>

<h1>Alerts</h1>
<nav class="page-tabs" aria-label="Alerts">
  <a href="#/alerts" aria-current={tab === '' ? 'page' : undefined}>New alerts</a>
  {#if canMaps}<a href="#/alerts?tab=emergencies" aria-current={tab === 'emergencies' ? 'page' : undefined}>Emergencies</a>{/if}
  <a href="#/alerts?tab=settings" aria-current={settingsTab ? 'page' : undefined}>Keywords and email</a>
</nav>

{#if settingsTab}
  <AlertSettings />
{:else if tab === 'emergencies' && canMaps}
  <Emergencies />
{:else}
<p class="small">Calls and ACARS messages that match your keywords, or that someone marked important{canMaps ? ', and the aircraft and ship emergencies you chose' : ''}{data ? `, since ${formatTime(data.since)}` : ''}.</p>
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
        {#if item.kind === 'emergency'}
          <div class="st-bad">{item.what === 'adsb' ? 'Aircraft' : 'Ship'} emergency: {item.text}</div>
          <div class="small dim">{item.distance_km != null ? `${Math.round(item.distance_km)} km from ${item.source_name}` : `Heard by ${item.source_name}`}{item.altitude != null ? `, ${Math.round(item.altitude)} ft` : ''}{item.ended_at ? `, ended ${formatTime(item.ended_at)}` : ', still showing'}</div>
          <div class="small"><a href={item.ended_at ? '#/alerts?tab=emergencies' : '#/maps'}>{item.ended_at ? 'All emergencies' : 'See it on the map'}</a></div>
        {:else if item.kind === 'call'}
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
{/if}
