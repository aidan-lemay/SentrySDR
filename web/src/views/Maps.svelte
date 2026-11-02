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
    for (const item of located) {
      L.circleMarker([item.lat, item.lon], { radius: 7, color: '#a80000', weight: 2, fillOpacity: 0.5 })
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

<h1>Maps</h1>
{#if error}<div class="err" role="alert">{error}</div>{/if}
<div class="buttons">
  <button aria-pressed={kind === 'adsb'} style:font-weight={kind === 'adsb' ? 'bold' : 'normal'} onclick={() => choose('adsb')}>Aircraft</button>
  <button aria-pressed={kind === 'ais'} style:font-weight={kind === 'ais' ? 'bold' : 'normal'} onclick={() => choose('ais')}>Ships</button>
  {#if user.tar1090_available}<a class="button" href="api/tar1090/index.html" target="_blank" rel="noopener">Open tar1090</a>{/if}
</div>
<div class="box">
  <div class="map" bind:this={host}></div>
</div>
<p class="small">
  {located.length} {kind === 'adsb' ? 'aircraft' : 'ships'} with a position{items.length !== located.length ? `, ${items.length - located.length} without` : ''}.
  {#if !items.length}Assign an {kind === 'adsb' ? 'ADS-B' : 'AIS'} target to a source to see traffic here.{/if}
</p>
