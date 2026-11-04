<script>
  import { untrack } from 'svelte';
  import { api, formatMHz, formatTime, formatDuration, formatSize } from '../lib.js';

  let { user, route } = $props();
  const PAGE = 25;
  const can = (permission) => user.permissions.includes(permission);

  let items = $state([]);
  let total = $state(0);
  let offset = $state(0);
  let search = $state('');
  let start = $state('');
  let end = $state('');
  let protectedOnly = $state(false);
  let error = $state('');
  let loaded = $state(false);
  let transcribing = $state({});
  let requested = $state({});

  const sourceFilter = $derived(new URLSearchParams(route.split('?')[1] ?? '').get('source_id'));

  async function load() {
    const params = new URLSearchParams({ limit: PAGE, offset });
    if (search) params.set('search', search);
    if (sourceFilter) params.set('source_id', sourceFilter);
    if (start) params.set('start', new Date(start).getTime() / 1000);
    if (end) params.set('end', new Date(end).getTime() / 1000);
    if (protectedOnly) params.set('protected', 'true');
    try {
      const reply = await api(`recordings?${params}`);
      items = reply.items;
      total = reply.total;
      error = '';
    } catch (failure) {
      error = failure.message;
    } finally {
      loaded = true;
    }
  }

  // Reload when the route's source changes, but not on each filter keystroke.
  $effect(() => {
    sourceFilter;
    untrack(() => {
      offset = 0;
      load();
    });
  });

  function applyFilters(event) {
    event.preventDefault();
    offset = 0;
    load();
  }

  function page(delta) {
    offset = Math.max(0, offset + delta);
    load();
    scrollTo(0, 0);
  }

  async function setProtected(call, value) {
    try {
      await api(`recordings/${call.id}/protect`, { method: 'POST', body: { protected: value } });
      call.protected = value;
    } catch (failure) {
      error = failure.message;
    }
  }

  async function remove(call) {
    if (!confirm(`Permanently delete this recording of ${call.target_name}?`)) return;
    try {
      await api(`recordings/${call.id}`, { method: 'DELETE' });
      await load();
    } catch (failure) {
      error = failure.message;
    }
  }

  const uploadedToClearcut = (call) => call.clearcut_system && call.uploads.some((u) => u.destination === 'clearcut' && u.status === 'done');

  async function requestTranscript(call) {
    transcribing[call.id] = true;
    error = '';
    try {
      const reply = await api(`recordings/${call.id}/transcribe`, { method: 'POST' });
      call.transcript = reply.transcript;
      if (!reply.transcript) requested[call.id] = true;
    } catch (failure) {
      error = failure.message;
    } finally {
      delete transcribing[call.id];
    }
  }

  const UPLOAD_LABEL = { clearcut: 'ClearCut', rdio: 'RDIO', broadcastify: 'Broadcastify' };
  const uploadClass = (status) => ({ done: 'st-ok', failed: 'st-bad' }[status] ?? 'st-warn');
</script>

<h1>Recordings</h1>
<p class="small">
  {total} call{total === 1 ? '' : 's'}{#if sourceFilter}, from one source. <a href="#/recordings">Show all</a>{/if}
  {#if user.clearcut_page_url} | <a href={user.clearcut_page_url} target="_blank" rel="noopener noreferrer">Open ClearCut</a>{/if}
</p>

{#if error}<div class="err" role="alert">{error}</div>{/if}

<form onsubmit={applyFilters}>
  <fieldset>
    <legend>Search</legend>
    <label class="field"><span>Target, source or transcript</span><input type="search" bind:value={search} /></label>
    <details>
      <summary class="small" style="padding:.3rem 0">Date and other filters</summary>
      <label class="field"><span>From</span><input type="datetime-local" bind:value={start} /></label>
      <label class="field"><span>To</span><input type="datetime-local" bind:value={end} /></label>
      <div class="field"><span>Protected</span><label class="tick"><input type="checkbox" bind:checked={protectedOnly} /> Protected calls only</label></div>
    </details>
    <div class="buttons form-buttons"><button class="main">Search</button></div>
  </fieldset>
</form>

<div class="box">
  {#each items as call (call.id)}
    <div class="call">
      <div><strong>{call.target_name || 'Unknown target'}</strong> <span class="mono small nowrap">{formatMHz(call.frequency_hz)}</span></div>
      <div class="small">{formatTime(call.started_at)}, {formatDuration(call.duration)}, {formatSize(call.size_bytes)}</div>
      <div class="small dim">{call.source_name} on {call.node_name}</div>
      {#if call.protected || call.uploads.length}
        <div class="small flags">
          {#if call.protected}<span class="st-ok">Protected</span>{/if}
          {#each call.uploads as upload}
            <span class={uploadClass(upload.status)} title={upload.detail || null}>{UPLOAD_LABEL[upload.destination]}: {upload.detail?.startsWith('Skipped') ? 'skipped' : upload.status}</span>
            {#if upload.detail && upload.status === 'failed'}<span class="st-bad">{upload.detail}</span>{/if}
          {/each}
        </div>
      {/if}
      <!-- svelte-ignore a11y_media_has_caption -->
      <audio src={`api/recordings/${call.id}/audio`} controls preload="none"></audio>
      {#if call.transcript}
        <div class="transcript">{call.transcript}</div>
      {:else if requested[call.id]}
        <p class="small dim">Transcript requested. It will appear here once ClearCut has finished.</p>
      {/if}
      <div class="buttons">
        {#if can('download')}<a class="button" href={`api/recordings/${call.id}/download`} download>Download</a>{/if}
        {#if can('transcribe') && uploadedToClearcut(call) && !call.transcript && !requested[call.id]}
          <button disabled={transcribing[call.id]} onclick={() => requestTranscript(call)}>{transcribing[call.id] ? 'Requesting...' : 'Get transcript'}</button>
        {/if}
        {#if can('protect')}
          <button onclick={() => setProtected(call, !call.protected)}>{call.protected ? 'Unprotect' : 'Protect'}</button>
        {/if}
        {#if user.role === 'admin' && !call.protected}
          <button class="bad" onclick={() => remove(call)}>Delete</button>
        {/if}
      </div>
    </div>
  {:else}
    {#if loaded}<p class="pad dim">No recordings found.</p>{/if}
  {/each}
</div>

{#if total > PAGE}
  <div class="pager">
    <button disabled={offset === 0} onclick={() => page(-PAGE)}>Newer</button>
    <span class="small">{offset + 1} to {Math.min(offset + PAGE, total)} of {total}</span>
    <button disabled={offset + PAGE >= total} onclick={() => page(PAGE)}>Older</button>
  </div>
{/if}
