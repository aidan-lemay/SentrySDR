<script>
  import { onMount, untrack } from 'svelte';
  import { api, formatMHz, formatTime } from '../lib.js';
  import Pager from '../Pager.svelte';

  let { route, user } = $props();
  const canMark = user.permissions.includes('protect');
  const PAGE = 50;

  let items = $state([]);
  let total = $state(0);
  let offset = $state(0);
  let search = $state('');
  let showEmpty = $state(false);
  let importantOnly = $state(false);
  let start = $state('');
  let end = $state('');
  let order = $state('desc');
  let error = $state('');
  let loaded = $state(false);

  const tailFilter = $derived(new URLSearchParams(route.split('?')[1] ?? '').get('tail'));
  const idFilter = $derived(new URLSearchParams(route.split('?')[1] ?? '').get('id'));  // from an alert email

  // The filters last searched with. Polling uses these, not what's half typed.
  const DEFAULTS = { search: '', showEmpty: false, importantOnly: false, start: '', end: '', order: 'desc' };
  let applied = $state({ ...DEFAULTS });
  // Anything typed, ticked or linked that isn't the default view: Clear then shows.
  const filtered = $derived(!!(tailFilter || idFilter || search || showEmpty || importantOnly || start || end || order !== 'desc'));
  let generation = 0;

  async function load() {
    const params = new URLSearchParams({ limit: PAGE, offset });
    if (applied.search) params.set('search', applied.search);
    if (applied.showEmpty) params.set('show_empty', 'true');
    if (applied.importantOnly) params.set('important', 'true');
    if (applied.start) params.set('start', new Date(applied.start).getTime() / 1000);
    if (applied.end) params.set('end', new Date(applied.end).getTime() / 1000);
    if (applied.order !== 'desc') params.set('order', applied.order);
    if (tailFilter) params.set('tail', tailFilter);
    if (idFilter) params.set('id', idFilter);
    const mine = ++generation;
    try {
      const reply = await api(`messages?${params}`);
      if (mine !== generation) return;
      items = reply.items;
      total = reply.total;
      error = '';
    } catch (failure) {
      if (mine === generation) error = failure.message;
    } finally {
      loaded = true;
    }
  }

  $effect(() => {
    tailFilter;
    idFilter;
    untrack(() => {
      offset = 0;
      load();
    });
  });

  function applyFilters(event) {
    event.preventDefault();
    applied = { search, showEmpty, importantOnly, start, end, order };
    offset = 0;
    load();
  }

  function clearFilters() {
    ({ search, showEmpty, importantOnly, start, end, order } = DEFAULTS);
    applied = { ...DEFAULTS };
    offset = 0;
    if (tailFilter || idFilter) location.hash = '#/messages';  // the route change reloads
    else load();
  }

  async function setImportant(message, value) {
    try {
      message.important = (await api(`messages/${message.id}/important`, { method: 'POST', body: { important: value } })).important;
    } catch (failure) {
      error = failure.message;
    }
  }

  function page(delta, top = false) {
    offset = Math.max(0, offset + delta);
    load();
    if (!top) scrollTo(0, 0);
  }

  // New messages arrive on the first page by themselves; older pages stay still.
  let polling = false;
  async function poll() {
    if (polling || offset !== 0 || applied.order !== 'desc' || document.hidden) return;
    polling = true;
    try {
      await load();
    } finally {
      polling = false;
    }
  }

  onMount(() => {
    const timer = setInterval(poll, 5000);
    return () => clearInterval(timer);
  });
</script>

<h1>Messages</h1>
<p class="small">
  {total} ACARS message{total === 1 ? '' : 's'}{#if idFilter}, the one from your alert email. <a href="#/messages">Show all</a>{:else if tailFilter}, from <span class="mono">{tailFilter}</span>. <a href="#/messages">Show all</a>{/if}
</p>

{#if error}<div class="err" role="alert">{error}</div>{/if}

<form onsubmit={applyFilters}>
  <div class="search">
    <div class="pair">
      <input type="search" bind:value={search} aria-label="Search messages" placeholder="Flight, registration, label or text" />
      <button class="main">Search</button>
      {#if filtered}<button type="button" onclick={clearFilters}>Clear</button>{/if}
    </div>
    <div class="flags">
      <label class="tick small"><input type="checkbox" bind:checked={showEmpty} onchange={applyFilters} /> Show messages with no text</label>
      <label class="tick small"><input type="checkbox" bind:checked={importantOnly} onchange={applyFilters} /> Important only</label>
    </div>
    <label class="field"><span>Order</span>
      <select bind:value={order} onchange={applyFilters}>
        <option value="desc">Newest first</option>
        <option value="asc">Oldest first</option>
      </select>
    </label>
    <details open={!!(applied.start || applied.end)}>
      <summary class="small">Date range</summary>
      <label class="field"><span>From</span><input type="datetime-local" bind:value={start} /></label>
      <label class="field"><span>To</span><input type="datetime-local" bind:value={end} /></label>
      <div class="buttons form-buttons"><button class="main">Apply dates</button></div>
    </details>
  </div>
</form>

<Pager {offset} {total} size={PAGE} order={applied.order} onpage={(delta) => page(delta, true)} />

<div class="box">
  {#each items as message (message.id)}
    <div class="call" class:important={message.important || message.matches.length}>
      <div>
        <strong class="mono">{message.flight || 'No flight'}</strong>
        {#if message.tail}<a class="mono small" href={`#/messages?tail=${encodeURIComponent(message.tail)}`}>{message.tail}</a>{/if}
        {#if message.label}<span class="small dim">label {message.label}</span>{/if}
      </div>
      <div class="small">{formatTime(message.received_at)}, <span class="mono">{formatMHz(message.frequency_hz)}</span>{message.level != null ? `, ${message.level.toFixed(1)} dB` : ''}</div>
      <div class="small dim">{message.source_name || message.serial}{message.node_name ? ` on ${message.node_name}` : ''}{message.fed ? ', sent to airframes.io' : ''}</div>
      {#if message.matches.length || message.important}
        <div class="small">
          {#if message.matches.length}<span class="st-bad">Matches: {message.matches.join(', ')}</span>{/if}
          {#if message.important}{message.matches.length ? ' · ' : ''}<span class="st-bad">Important</span>{/if}
        </div>
      {/if}
      {#if canMark}
        <div class="buttons" style="margin:.2rem 0"><button onclick={() => setImportant(message, !message.important)}>{message.important ? 'Not important' : 'Mark important'}</button></div>
      {/if}
      {#if message.text}
        <pre class="message-text mono small">{message.text}</pre>
      {:else}
        <p class="small dim">No text.</p>
      {/if}
    </div>
  {:else}
    {#if loaded}<p class="pad dim">No messages found. Assign an ACARS target to an SDR on the Sources page.</p>{/if}
  {/each}
</div>

<Pager {offset} {total} size={PAGE} order={applied.order} onpage={(delta) => page(delta)} />
