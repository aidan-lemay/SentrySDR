<script>
  import { onMount } from 'svelte';
  import L from 'leaflet';
  import { api } from '../lib.js';

  let { user } = $props();
  let kind = $state('adsb');
  let items = $state([]);
  let error = $state('');
  let host;
  let map;
  let layer;
  let fitted = false;

  const located = $derived(items.filter((item) => Number.isFinite(item.lat) && Number.isFinite(item.lon)));

  const label = (item) => kind === 'adsb'
    ? (item.flight?.trim() || item.hex)
    : (item.shipname?.trim() || item.mmsi);

  // Remote data is only ever placed with textContent, never parsed as HTML.
  function popup(item) {
    const node = document.createElement('div');
    const lines = kind === 'adsb'
      ? [label(item), `Hex ${item.hex}`, item.alt_baro != null ? `Altitude ${item.alt_baro} ft` : null, item.gs != null ? `Speed ${Math.round(item.gs)} kt` : null]
      : [label(item), `MMSI ${item.mmsi}`, item.speed != null ? `Speed ${item.speed} kt` : null, item.destination ? `To ${item.destination}` : null];
    for (const line of lines.filter(Boolean)) {
      const row = document.createElement('div');
      row.textContent = String(line);
      node.append(row);
    }
    return node;
  }

  function draw() {
    if (!layer) return;
    layer.clearLayers();
    const color = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
    for (const item of located) {
      L.circleMarker([item.lat, item.lon], { radius: 6, color, weight: 2, fillOpacity: 0.6 })
        .bindTooltip(String(label(item)))
        .bindPopup(() => popup(item))
        .addTo(layer);
    }
    if (located.length && !fitted) {
      map.fitBounds(L.latLngBounds(located.map((i) => [i.lat, i.lon])), { maxZoom: 10, padding: [30, 30] });
      fitted = true;
    }
  }

  async function refresh() {
    try {
      items = (await api(`maps/${kind}`)).items;
      error = '';
      draw();
    } catch (failure) {
      error = failure.message;
    }
  }

  function choose(next) {
    kind = next;
    fitted = false;
    items = [];
    draw();
    refresh();
  }

  onMount(() => {
    map = L.map(host, { worldCopyJump: true }).setView([39.5, -98.35], 4);
    L.tileLayer(user.map_tile_url, {
      maxZoom: 18,
      attribution: '&copy; OpenStreetMap contributors',
      referrerPolicy: 'strict-origin-when-cross-origin',
    }).addTo(map);
    layer = L.layerGroup().addTo(map);
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => {
      clearInterval(timer);
      map.remove();
    };
  });
</script>

<div class="page-head">
  <h1>Maps</h1>
  <div class="row">
    <button class="tab" class:active={kind === 'adsb'} onclick={() => choose('adsb')}>Aircraft</button>
    <button class="tab" class:active={kind === 'ais'} onclick={() => choose('ais')}>Ships</button>
    {#if user.tar1090_available}<a href="api/tar1090/index.html" target="_blank" rel="noopener">Open tar1090</a>{/if}
  </div>
</div>
{#if error}<div class="notice error" role="alert">{error}</div>{/if}
<section class="panel">
  <div class="map" bind:this={host}></div>
  <p class="muted small" style="margin-bottom:0">
    {located.length} {kind === 'adsb' ? 'aircraft' : 'ships'} with a position{items.length !== located.length ? `, ${items.length - located.length} without` : ''}.
    {#if !items.length}Assign an {kind === 'adsb' ? 'ADS-B' : 'AIS'} target to a source to see traffic here.{/if}
  </p>
</section>
