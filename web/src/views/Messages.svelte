<script>
  import { onMount, untrack } from 'svelte';
  import { api, formatMHz, formatTime } from '../lib.js';

  let { route } = $props();
  const PAGE = 50;

  let items = $state([]);
  let total = $state(0);
  let offset = $state(0);
  let search = $state('');
  let showEmpty = $state(false);
  let error = $state('');
  let loaded = $state(false);

  const tailFilter = $derived(new URLSearchParams(route.split('?')[1] ?? '').get('tail'));

  // The filters last searched with. Polling uses these, not what's half typed.
  let applied = { search: '', showEmpty: false };
  let generation = 0;

  async function load() {
    const params = new URLSearchParams({ limit: PAGE, offset });
    if (applied.search) params.set('search', applied.search);
    if (applied.showEmpty) params.set('show_empty', 'true');
    if (tailFilter) params.set('tail', tailFilter);
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
    untrack(() => {
      offset = 0;
      load();
    });
  });

  function applyFilters(event) {
    event.preventDefault();
    applied = { search, showEmpty };
    offset = 0;
    load();
  }

  function page(delta) {
    offset = Math.max(0, offset + delta);
    load();
    scrollTo(0, 0);
  }

  // New messages arrive on the first page by themselves; older pages stay still.
  let polling = false;
  async function poll() {
    if (polling || offset !== 0 || document.hidden) return;
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
  {total} ACARS message{total === 1 ? '' : 's'}{#if tailFilter}, from <span class="mono">{tailFilter}</span>. <a href="#/messages">Show all</a>{/if}
</p>

{#if error}<div class="err" role="alert">{error}</div>{/if}

<form onsubmit={applyFilters}>
  <fieldset>
    <legend>Search</legend>
    <label class="field"><span>Flight, registration, label or text</span><input type="search" bind:value={search} /></label>
    <div class="field"><span>Empty messages</span><label class="tick"><input type="checkbox" bind:checked={showEmpty} /> Show messages with no text</label></div>
    <div class="buttons form-buttons"><button class="main">Search</button></div>
  </fieldset>
</form>

<div class="box">
  {#each items as message (message.id)}
    <div class="call">
      <div>
        <strong class="mono">{message.flight || 'No flight'}</strong>
        {#if message.tail}<a class="mono small" href={`#/messages?tail=${encodeURIComponent(message.tail)}`}>{message.tail}</a>{/if}
        {#if message.label}<span class="small dim">label {message.label}</span>{/if}
      </div>
      <div class="small">{formatTime(message.received_at)}, <span class="mono">{formatMHz(message.frequency_hz)}</span>{message.level != null ? `, ${message.level.toFixed(1)} dB` : ''}</div>
      <div class="small dim">{message.source_name || message.serial}{message.node_name ? ` on ${message.node_name}` : ''}{message.fed ? ', sent to airframes.io' : ''}</div>
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

{#if total > PAGE}
  <div class="pager">
    <button disabled={offset === 0} onclick={() => page(-PAGE)}>Newer</button>
    <span class="small">{offset + 1} to {Math.min(offset + PAGE, total)} of {total}</span>
    <button disabled={offset + PAGE >= total} onclick={() => page(PAGE)}>Older</button>
  </div>
{/if}
