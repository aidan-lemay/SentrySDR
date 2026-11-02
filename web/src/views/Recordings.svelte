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
    try {
      await api(`recordings/${call.id}`, { method: 'DELETE' });
      await load();
    } catch (failure) {
      error = failure.message;
    }
  }

  const UPLOAD_LABEL = { clearcut: 'ClearCut', rdio: 'RDIO' };
  const uploadClass = (status) => ({ done: 'ok', failed: 'bad' }[status] ?? 'warn');
</script>

<div class="page-head">
  <h1>Recordings</h1>
  <span class="muted small">
    {total} call{total === 1 ? '' : 's'}
    {#if sourceFilter}· one source · <a href="#/recordings">show all</a>{/if}
    {#if user.clearcut_page_url}· <a href={user.clearcut_page_url} target="_blank" rel="noopener noreferrer">Open ClearCut</a>{/if}
  </span>
</div>

{#if error}<div class="notice error" role="alert">{error}</div>{/if}

<form class="panel fields" onsubmit={applyFilters}>
  <label>Search <input type="search" bind:value={search} placeholder="Target, source or transcript" /></label>
  <label>From <input type="datetime-local" bind:value={start} /></label>
  <label>To <input type="datetime-local" bind:value={end} /></label>
  <label class="check"><input type="checkbox" bind:checked={protectedOnly} /> Protected only</label>
  <button class="primary">Apply</button>
</form>

<section class="panel">
  {#each items as call (call.id)}
    <article class="call">
      <div class="row spread">
        <div>
          <strong>{call.target_name || 'Unknown target'}</strong>
          <span class="mono small muted">{formatMHz(call.frequency_hz)}</span>
          {#if call.protected}<span class="pill ok">Protected</span>{/if}
          {#each call.uploads as upload}
            <span class="pill {uploadClass(upload.status)}">{UPLOAD_LABEL[upload.destination]}: {upload.status}</span>
          {/each}
        </div>
        <div class="muted small">{formatTime(call.started_at)} · {formatDuration(call.duration)} · {formatSize(call.size_bytes)}</div>
      </div>
      <div class="muted small">{call.source_name} on {call.node_name}</div>
      <div class="row" style="margin-top:.35rem">
        <!-- svelte-ignore a11y_media_has_caption -->
        <audio src={`api/recordings/${call.id}/audio`} controls preload="none"></audio>
        {#if can('download')}<a href={`api/recordings/${call.id}/download`} download>Download</a>{/if}
        {#if can('protect')}
          <button onclick={() => setProtected(call, !call.protected)}>{call.protected ? 'Unprotect' : 'Protect'}</button>
        {/if}
        {#if user.role === 'admin' && !call.protected}
          <button class="danger" onclick={() => remove(call)}>Delete</button>
        {/if}
      </div>
      {#if call.transcript}<div class="transcript">{call.transcript}</div>{/if}
    </article>
  {:else}
    {#if loaded}<div class="empty">No recordings match.</div>{/if}
  {/each}
</section>

{#if total > PAGE}
  <div class="row spread">
    <button disabled={offset === 0} onclick={() => page(-PAGE)}>Newer</button>
    <span class="muted small">{offset + 1}–{Math.min(offset + PAGE, total)} of {total}</span>
    <button disabled={offset + PAGE >= total} onclick={() => page(PAGE)}>Older</button>
  </div>
{/if}
