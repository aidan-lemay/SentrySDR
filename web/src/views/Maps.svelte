<script>
  import { onMount } from 'svelte';
  import L from 'leaflet';
  import { api } from '../lib.js';

  let { user } = $props();
  let kind = $state('adsb');
  let items = $state([]);
  let receivers = $state([]);
  let showTrails = $state(true);
  let showCoverage = $state(true);
  let error = $state('');
  let host;
  let map;
  let receiverLayer;
  let trailLayer;
  let markerLayer;
  let fitted = false;

  const located = $derived(items.filter((item) => Number.isFinite(item.lat) && Number.isFinite(item.lon)));
  const inEmergency = $derived(items.filter((item) => item.emergency));

  const label = (item) => kind === 'adsb'
    ? (item.flight?.trim() || item.hex)
    : (item.shipname?.trim() || item.mmsi);

  // Remote data is only ever placed with textContent, never parsed as HTML.
  function popup(item) {
    const node = document.createElement('div');
    const lines = kind === 'adsb'
      ? [label(item), item.registration ? `Registration ${item.registration}` : null, `Hex ${item.hex}`, item.alt_baro != null ? `Altitude ${item.alt_baro} ft` : null, item.gs != null ? `Speed ${Math.round(item.gs)} kt` : null]
      : [label(item), `MMSI ${item.mmsi}`, item.speed != null ? `Speed ${item.speed} kt` : null, item.destination ? `To ${item.destination}` : null];
    if (item.emergency) lines.unshift(`EMERGENCY: ${item.emergency}`);
    for (const line of lines.filter(Boolean)) {
      const row = document.createElement('div');
      row.textContent = String(line);
      node.append(row);
    }
    if (kind === 'adsb' && item.acars_count) {
      const link = document.createElement('a');
      link.href = `#/messages?tail=${encodeURIComponent(item.acars_tail)}`;
      link.textContent = `${item.acars_count} ACARS message${item.acars_count === 1 ? '' : 's'} in the last hour`;
      node.append(link);
    }
    return node;
  }

  // Markers are kept and moved, not redrawn, so an info box someone has open stays open
  // and shows the latest. Trails are redrawn each time; receivers only when they change.
  const markers = new Map();
  let receiversDrawn = '';
  const keyOf = (item) => String(kind === 'adsb' ? item.hex : item.mmsi);

  function drawReceivers(hue) {
    const signature = JSON.stringify([receivers, showCoverage, hue]);
    if (signature === receiversDrawn) return;
    receiversDrawn = signature;
    receiverLayer.clearLayers();
    for (const receiver of receivers) {
      if (showCoverage && receiver.coverage?.length) {
        L.polygon(receiver.coverage, { color: hue, weight: 1, fillOpacity: 0.12, interactive: false }).addTo(receiverLayer);
      }
      const node = document.createElement('div');
      for (const line of [receiver.name, receiver.farthest_km ? `Farthest aircraft: ${Math.round(receiver.farthest_km)} km` : null].filter(Boolean)) {
        const row = document.createElement('div');
        row.textContent = line;
        node.append(row);
      }
      L.marker([receiver.lat, receiver.lon], {
        icon: L.divIcon({ className: 'receiver-mark', iconSize: [14, 14] }), keyboard: true, title: receiver.name,
      }).bindPopup(node).addTo(receiverLayer);
    }
  }

  function place(item, hue) {
    const key = keyOf(item);
    let marker = markers.get(key);
    if (!marker) {
      marker = L.circleMarker([item.lat, item.lon]);
      marker.bindPopup(() => popup(marker.item));
      // The map pans to show an info box when it opens, but not again as the aircraft moves on.
      marker.on('popupopen', (event) => { event.popup.options.autoPan = false; });
      marker.on('popupclose', (event) => { event.popup.options.autoPan = true; });
      marker.addTo(markerLayer);
      markers.set(key, marker);
    } else {
      marker.setLatLng([item.lat, item.lon]);
    }
    marker.item = item;
    // Red is kept for emergencies, which also keep their label showing.
    const tip = item.emergency ? `${label(item)}: ${item.emergency.split(' (')[0]}` : String(label(item));
    const look = `${item.emergency ? 'emergency' : hue}|${tip}`;
    if (marker.look !== look) {
      marker.look = look;
      marker.setStyle(item.emergency
        ? { color: '#c00000', weight: 4, fillColor: '#ff3030', fillOpacity: 0.8 }
        : { color: hue, weight: 2, fillColor: hue, fillOpacity: 0.5 });
      marker.setRadius(item.emergency ? 11 : 7);
      marker.unbindTooltip();
      marker.bindTooltip(tip, item.emergency ? { permanent: true, direction: 'top', className: 'emergency-label' } : {});
    }
    if (marker.isPopupOpen()) marker.getPopup().update();
    if (item.emergency) marker.bringToFront();
  }

  function draw() {
    if (!markerLayer) return;
    const hue = getComputedStyle(document.documentElement).getPropertyValue('--viz').trim() || '#2a78d6';
    drawReceivers(hue);
    trailLayer.clearLayers();
    const here = new Set();
    for (const item of located) {
      if (kind === 'adsb' && showTrails && item.trail?.length > 1) {
        L.polyline(item.trail, { color: hue, weight: 2, opacity: 0.6, interactive: false }).addTo(trailLayer);
      }
      here.add(keyOf(item));
      place(item, hue);
    }
    for (const [key, marker] of markers) {
      if (!here.has(key)) {
        markerLayer.removeLayer(marker);
        markers.delete(key);
      }
    }
    if (located.length && !fitted) {
      map.fitBounds(L.latLngBounds(located.map((i) => [i.lat, i.lon])), { maxZoom: 10, padding: [30, 30] });
      fitted = true;
    }
  }

  let generation = 0;
  async function refresh() {
    const mine = ++generation;
    try {
      const reply = await api(`maps/${kind}`);
      if (mine !== generation) return;  // switched map, or a newer refresh, while this one was out
      items = reply.items;
      receivers = reply.receivers ?? [];
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
    markerLayer.clearLayers();
    markers.clear();
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
    receiverLayer = L.layerGroup().addTo(map);
    trailLayer = L.layerGroup().addTo(map);
    markerLayer = L.layerGroup().addTo(map);
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
{#if inEmergency.length}
  <div class="err" role="status">
    {#each inEmergency as item (kind === 'adsb' ? item.hex : item.mmsi)}
      <div><strong>Emergency:</strong> {label(item)}, {item.emergency}{Number.isFinite(item.lat) ? '' : ' (no position)'}</div>
    {/each}
    <a href="#/alerts?tab=emergencies">All emergencies</a>
  </div>
{/if}
<div class="buttons">
  <button aria-pressed={kind === 'adsb'} style:font-weight={kind === 'adsb' ? 'bold' : 'normal'} onclick={() => choose('adsb')}>Aircraft</button>
  <button aria-pressed={kind === 'ais'} style:font-weight={kind === 'ais' ? 'bold' : 'normal'} onclick={() => choose('ais')}>Ships</button>
  {#if user.tar1090_available}<a class="button" href="api/tar1090/index.html" target="_blank" rel="noopener">Open tar1090</a>{/if}
</div>
<div class="flags">
  {#if kind === 'adsb'}<label class="tick small"><input type="checkbox" bind:checked={showTrails} onchange={draw} /> Trails</label>{/if}
  {#if kind === 'adsb'}<label class="tick small"><input type="checkbox" bind:checked={showCoverage} onchange={draw} /> Coverage</label>{/if}
</div>
<div class="box">
  <div class="map" bind:this={host}></div>
</div>
<p class="small">
  {located.length} {kind === 'adsb' ? 'aircraft' : 'ships'} with a position{items.length !== located.length ? `, ${items.length - located.length} without` : ''}.
  {#if !items.length}Assign an {kind === 'adsb' ? 'ADS-B' : 'AIS'} target to a source to see traffic here.{/if}
  {#if receivers.length}Squares are receivers{kind === 'adsb' ? ', and each outline is how far one has heard aircraft' : ''}.{:else if user.role === 'admin'}Give an SDR a location under Admin &gt; SDR sources to show it here.{/if}
</p>
