<script>
  // Every aircraft and ship emergency recorded, newest first, whatever anyone chose to be told about.
  import { onMount } from 'svelte';
  import { api, formatTime } from '../lib.js';
  import Pager from '../Pager.svelte';

  const PAGE = 50;
  let items = $state([]);
  let total = $state(0);
  let offset = $state(0);
  let kind = $state('');
  let order = $state('desc');
  let error = $state('');
  let loaded = $state(false);

  async function load() {
    try {
      const reply = await api(`emergencies?${new URLSearchParams({ kind, order, limit: PAGE, offset })}`);
      items = reply.items;
      total = reply.total;
      error = '';
    } catch (failure) {
      error = failure.message;
    } finally {
      loaded = true;
    }
  }

  function choose(changes) {
    ({ kind, order } = { kind, order, ...changes });
    offset = 0;
    load();
  }

  function page(delta, top = false) {
    offset = Math.max(0, offset + delta);
    load();
    if (!top) scrollTo(0, 0);
  }

  const where = (e) => [
    e.lat != null && e.lon != null ? `${e.lat.toFixed(4)}, ${e.lon.toFixed(4)}` : null,
    e.altitude != null ? `${Math.round(e.altitude)} ft` : null,
    e.distance_km != null ? `${Math.round(e.distance_km)} km from ${e.source_name}` : `heard by ${e.source_name}`,
  ].filter(Boolean).join(', ');

  onMount(() => {
    load();
    const timer = setInterval(() => offset === 0 && order === 'desc' && !document.hidden && load(), 15000);
    return () => clearInterval(timer);
  });
</script>

<p class="small">Every emergency any receiver has seen, kept as long as ACARS messages. A squawk counts once an aircraft has shown it for 10 seconds; AIS beacons and statuses count at once. Choose which ones alert you under Keywords and email.</p>
{#if error}<div class="err" role="alert">{error}</div>{/if}
<label class="field"><span>Show</span>
  <select value={kind} onchange={(event) => choose({ kind: event.target.value })}>
    <option value="">Aircraft and ships</option>
    <option value="adsb">Aircraft</option>
    <option value="ais">Ships</option>
  </select>
</label>
<label class="field"><span>Order</span>
  <select value={order} onchange={(event) => choose({ order: event.target.value })}>
    <option value="desc">Newest first</option>
    <option value="asc">Oldest first</option>
  </select>
</label>
{#if kind || order !== 'desc'}<div class="buttons form-buttons"><button type="button" onclick={() => choose({ kind: '', order: 'desc' })}>Clear</button></div>{/if}
<Pager {offset} {total} size={PAGE} {order} onpage={(delta) => page(delta, true)} />
<div class="box">
  {#each items as e (e.id)}
    <div class="call" class:important={!e.ended_at}>
      <div><strong>{e.title}</strong> <span class="small mono dim">{e.kind === 'adsb' ? 'ICAO' : 'MMSI'} {e.ident.toUpperCase()}</span></div>
      <div class="st-bad">{e.what}</div>
      <div class="small">{formatTime(e.first_seen)} to {e.ended_at ? formatTime(e.ended_at) : 'now, still showing'}</div>
      <div class="small dim">{where(e)}</div>
      {#if !e.ended_at}<div class="small"><a href="#/maps">See it on the map</a></div>{/if}
    </div>
  {:else}
    {#if loaded}<p class="pad dim">None recorded.</p>{/if}
  {/each}
</div>
<Pager {offset} {total} size={PAGE} {order} onpage={(delta) => page(delta)} />
