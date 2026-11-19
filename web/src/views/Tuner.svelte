<script>
  // The web tuner. Without ?source= it lists the SDRs that are tuners now, inside the site.
  // With it, it's one tuner in the whole window, laid out like GQRX: the frequency across the top,
  // spectrum and waterfall on the left, and a tabbed panel of settings on the right.
  import { onMount } from 'svelte';
  import { api } from '../lib.js';
  import FreqReadout from '../FreqReadout.svelte';
  import TunerBookmarks from '../TunerBookmarks.svelte';

  let { route, user } = $props();
  const sourceId = $derived(Number(new URLSearchParams(route.split('?')[1] ?? '').get('source')) || null);

  // Keep in step with sentrysdr/tuner.py: id, label, usual width, narrowest, widest.
  const MODES = [
    ['nfm', 'NFM', 12500, 2500, 25000], ['am', 'AM', 10000, 2000, 20000], ['wfm', 'WFM mono', 180000, 50000, 220000],
    ['wfm_stereo', 'WFM stereo', 180000, 120000, 220000], ['usb', 'USB', 2700, 500, 5000], ['lsb', 'LSB', 2700, 500, 5000],
    ['cw', 'CW', 500, 100, 2000],
  ];
  const RATES = [250000, 1024000, 1536000, 1800000, 2048000, 2400000];
  const STEPS = [10, 100, 1000, 5000, 6250, 10000, 12500, 25000, 100000];
  const DB_FLOOR = -130;
  const HISTORY = 400;  // waterfall rows kept: about half a minute
  const EDGE_PX = 6;  // how close to a passband edge a drag grabs the edge

  // --- the list of tuners ---------------------------------------------------------
  let tuners = $state(null);
  let listError = $state('');
  async function loadTuners() {
    try {
      tuners = await api('tuners');
      listError = '';
    } catch (failure) {
      listError = failure.message;
    }
  }

  // --- one tuner ----------------------------------------------------------------------
  const saved = (() => {
    try { return JSON.parse(localStorage.getItem('sentrysdr-tuner') || '{}'); } catch { return {}; }
  })();
  let listen = $state({ frequency_hz: 0, mode: saved.mode ?? 'nfm', bandwidth_hz: null, deemphasis_us: saved.deemphasis_us ?? 75 });
  let step = $state(saved.step ?? 12500);
  let volume = $state(saved.volume ?? 0.8);
  let muted = $state(false);
  let range = $state({ low: saved.low ?? -110, high: saved.high ?? -50 });
  // A range you set is kept for next time. Until then, the first rows choose one, and that isn't kept.
  let rangeChosen = saved.low !== undefined;
  let rangeSet = rangeChosen;
  let split = $state(saved.split ?? 35);  // the spectrum's share of the height, in percent
  let averaging = $state(saved.averaging ?? 0.4);  // how much each new row counts in the spectrum line
  let squelchOn = $state(false);
  let squelchLevel = $state(-80);
  let tab = $state('receiver');

  let sdr = $state({ running: false });
  let rx = $state({ level_db: DB_FLOOR, open: false, inside: true, stereo: false, rds: null });
  let error = $state('');
  let fatal = $state('');
  let sourceName = $state('');
  let connected = $state(false);
  let audioOn = $state(false);
  let gotoText = $state('');
  let centerText = $state('');
  let centerEdited = false;  // typed but not set yet: updates from the server leave it alone
  let hover = $state(null);  // { x, y, hz, db, grab } under the pointer
  let gainAuto = $state(true);
  let gainValue = $state(30);
  let handTo = $state('');
  let dragging = $state(null);  // 'move' | 'low' | 'high' while a drag is under way

  let socket = null;
  let retry = null;
  let audio = null;  // { context, gain, next }
  let spectrumCanvas = $state();
  let waterfallCanvas = $state();
  let plots = $state();
  let rows = [];  // [{ center, rate, data }], newest first
  let latest = null;
  let smooth = null;
  let drag = null;  // { kind, startX, startHz, startFreq, moved }
  let pending = null;  // a listen change waiting out the send interval while dragging

  const span = $derived(sdr.rate ? { low: sdr.center_hz - sdr.rate / 2, high: sdr.center_hz + sdr.rate / 2 } : null);
  const modeInfo = $derived(MODES.find(([id]) => id === listen.mode) ?? MODES[0]);
  const bandwidth = $derived(listen.bandwidth_hz ?? modeInfo[2]);
  const owner = $derived(!!sdr.you_control);

  function remember() {
    try {
      localStorage.setItem('sentrysdr-tuner', JSON.stringify({ mode: listen.mode, deemphasis_us: listen.deemphasis_us, step, volume, split, averaging,
        ...(rangeSet ? { low: range.low, high: range.high } : {}) }));
      if (listen.frequency_hz) localStorage.setItem(`sentrysdr-tuner-${sourceId}`, String(listen.frequency_hz));
    } catch { /* private window: not remembered */ }
  }

  const send = (message) => socket?.readyState === 1 && socket.send(JSON.stringify(message));
  const mhz = (hz) => (hz / 1e6).toFixed(hz % 1000 ? 4 : 3);

  function parseMHz(text) {
    const value = Number(String(text).trim().replace(',', '.'));
    return Number.isFinite(value) && value > 0 ? Math.round(value * 1e6) : null;
  }

  // The passband, from the channel's frequency: one-sided for sideband modes.
  function passband(hz = listen.frequency_hz, bw = bandwidth) {
    if (listen.mode === 'usb') return [hz, hz + bw];
    if (listen.mode === 'lsb') return [hz - bw, hz];
    return [hz - bw / 2, hz + bw / 2];
  }

  function sendListen() {
    if (!listen.frequency_hz) return;
    send({ t: 'listen', ...listen, squelch_db: squelchOn ? squelchLevel : null });
    remember();
  }

  // While dragging, at most ten updates a second reach the server; the last one always does.
  function sendListenSoon() {
    if (pending) return;
    pending = setTimeout(() => {
      pending = null;
      sendListen();
    }, 100);
  }

  // Your channel to a frequency. Outside the span, the owner's span follows, as in GQRX.
  function tuneTo(hz, { snap = false } = {}) {
    if (!hz) return;
    const target = snap ? Math.round(hz / step) * step : Math.round(hz);
    if (span && (target < span.low || target > span.high)) {
      if (!owner) {
        error = `${mhz(target)} MHz is outside this SDR's span. ${sdr.controller ? `${sdr.controller} owns the tuner and can move it.` : 'Whoever takes the tuner can move it.'}`;
        return;
      }
      send({ t: 'center', hz: Math.round(target - sdr.rate / 4) });
    }
    listen.frequency_hz = target;
    sendListen();
  }

  function setMode(mode) {
    listen.mode = mode;
    listen.bandwidth_hz = null;  // each mode starts at its own usual width
    sendListen();
  }

  function setBandwidth(hz) {
    listen.bandwidth_hz = Math.round(Math.min(modeInfo[4], Math.max(modeInfo[3], hz)));
    sendListen();
  }

  function setCenter(hz, { moveChannel = true } = {}) {
    if (!owner) return;
    if (!hz) {
      error = 'Type a centre frequency in MHz, like 146.52.';
      return;
    }
    centerEdited = false;
    // As in GQRX, your channel moves with the span, staying where it was within it.
    if (moveChannel && span && listen.frequency_hz >= span.low && listen.frequency_hz <= span.high) {
      listen.frequency_hz += hz - sdr.center_hz;
      sendListen();
    }
    send({ t: 'center', hz });
  }

  function pick(bookmark) {
    listen.mode = bookmark.mode;
    listen.bandwidth_hz = bookmark.bandwidth_hz ?? null;
    tuneTo(bookmark.frequency_hz);
  }

  // --- the connection ---------------------------------------------------------------------
  function connect() {
    clearTimeout(retry);
    const url = new URL(`api/tuner/${sourceId}/ws`, location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';
    socket.onopen = () => { connected = true; error = ''; };
    socket.onmessage = (event) => (typeof event.data === 'string' ? onText(JSON.parse(event.data)) : onFrame(event.data));
    socket.onclose = () => {
      connected = false;
      if (!fatal) retry = setTimeout(connect, 2000);
    };
  }

  function onText(message) {
    if (message.t === 'hello') sourceName = message.source;
    else if (message.t === 'error') {
      if (message.fatal) fatal = message.detail;
      else error = message.detail;
    } else if (message.t === 'rx') rx = message;
    else if (message.t === 'state') {
      const first = !sdr.rate && message.rate;
      sdr = message;
      if (!message.you_control && tab === 'input') tab = 'receiver';
      if (message.center_hz && !centerEdited) centerText = mhz(message.center_hz);
      if (message.gain_db === null) gainAuto = true;
      else if (message.gain_db !== undefined) { gainAuto = false; gainValue = message.gain_db; }
      if (first) {
        // Back where you were last time, if it's still in the span; otherwise the middle.
        const last = Number(localStorage.getItem(`sentrysdr-tuner-${sourceId}`));
        const low = message.center_hz - message.rate / 2, high = message.center_hz + message.rate / 2;
        listen.frequency_hz = last >= low && last <= high ? last : message.center_hz;
        sendListen();
      }
    }
  }

  function onFrame(buffer) {
    const view = new DataView(buffer);
    const kind = String.fromCharCode(view.getUint8(0));
    if (kind === 'W') {
      const center = Number(view.getBigUint64(1, true));
      const rate = view.getUint32(9, true);
      const data = new Uint8Array(buffer, 13);
      rows.unshift({ center, rate, data });
      if (rows.length > HISTORY) rows.length = HISTORY;
      latest = data;
      smooth = smooth && smooth.length === data.length ? smooth.map((v, i) => v * (1 - averaging) + data[i] * averaging) : Float32Array.from(data);
      if (!rangeChosen && rows.length === 12) {
        rangeChosen = true;
        autoRange();
      }
      drawRow(data);
      drawSpectrum();
    } else if (kind === 'A' && audio) {
      const rate = view.getFloat32(5, true);
      const channels = view.getUint8(9);
      play(new Uint8Array(buffer, 15), rate, channels);
    }
  }

  // --- audio ------------------------------------------------------------------------------
  const MULAW = new Float32Array(256);
  for (let code = 0; code < 256; code++) {
    const negative = code < 128;
    const magnitude = (negative ? 127 - code : 255 - code) / 127;
    const x = (Math.pow(256, magnitude) - 1) / 255;
    MULAW[code] = negative ? -x : x;
  }

  function startAudio() {
    const context = new AudioContext();
    const gain = context.createGain();
    gain.gain.value = muted ? 0 : volume;
    gain.connect(context.destination);
    audio = { context, gain, next: 0 };
    audioOn = true;
  }

  function stopAudio() {
    audio?.context.close();
    audio = null;
    audioOn = false;
  }

  function play(bytes, rate, channels) {
    const { context } = audio;
    const frames = Math.floor(bytes.length / channels);
    if (!frames) return;
    const buffer = context.createBuffer(channels, frames, rate);
    for (let c = 0; c < channels; c++) {
      const out = buffer.getChannelData(c);
      for (let i = 0; i < frames; i++) out[i] = MULAW[bytes[i * channels + c]];
    }
    const now = context.currentTime;
    // A quarter of a second in hand smooths out the network. Fallen behind: start again a little ahead.
    // Too far ahead (the tab was asleep): drop this piece so the delay doesn't keep growing.
    if (audio.next < now + 0.05) audio.next = now + 0.25;
    if (audio.next > now + 1.0) return;
    const node = context.createBufferSource();
    node.buffer = buffer;
    node.connect(audio.gain);
    node.start(audio.next);
    audio.next += buffer.duration;
  }

  $effect(() => {
    if (audio) audio.gain.gain.value = muted ? 0 : volume;
  });

  // --- drawing ------------------------------------------------------------------------------
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  let colours = null;  // 256 entries of [r, g, b] for this contrast

  // One hue, from the box colour at the bottom of the range to --waterfall at the top.
  function buildColours() {
    const [low, high] = [rgb(css('--box') || '#ffffff'), rgb(css('--waterfall') || '#0b3d91')];
    colours = new Uint8ClampedArray(256 * 3);
    for (let code = 0; code < 256; code++) {
      const db = code / 2 + DB_FLOOR;
      const t = Math.min(1, Math.max(0, (db - range.low) / Math.max(1, range.high - range.low)));
      for (let c = 0; c < 3; c++) colours[code * 3 + c] = low[c] + (high[c] - low[c]) * t;
    }
  }

  function paintRow(context, data, y) {
    const image = context.createImageData(data.length, 1);
    for (let i = 0; i < data.length; i++) {
      const code = data[i] * 3;
      image.data[i * 4] = colours[code];
      image.data[i * 4 + 1] = colours[code + 1];
      image.data[i * 4 + 2] = colours[code + 2];
      image.data[i * 4 + 3] = 255;
    }
    context.putImageData(image, 0, y);
  }

  function drawRow(data) {
    const canvas = waterfallCanvas;
    if (!canvas) return;
    if (canvas.width !== data.length) {
      canvas.width = data.length;
      redrawWaterfall();
      return;
    }
    const context = canvas.getContext('2d');
    if (!colours) buildColours();
    context.drawImage(canvas, 0, 0, canvas.width, canvas.height - 1, 0, 1, canvas.width, canvas.height - 1);
    paintRow(context, data, 0);
  }

  function redrawWaterfall() {
    const canvas = waterfallCanvas;
    if (!canvas) return;
    buildColours();
    const context = canvas.getContext('2d');
    context.fillStyle = css('--box');
    context.fillRect(0, 0, canvas.width, canvas.height);
    rows.forEach((row, y) => row.data.length === canvas.width && paintRow(context, row.data, y));
  }

  // Frequency gridlines about every 110 pixels, on round numbers.
  function tickStep(width) {
    const raw = (span.high - span.low) / Math.max(2, width / 110);
    const magnitude = 10 ** Math.floor(Math.log10(raw));
    return [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw);
  }

  function drawSpectrum() {
    const canvas = spectrumCanvas;
    if (!canvas) return;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    const scale = devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * scale) || canvas.height !== Math.round(height * scale)) {
      canvas.width = Math.round(width * scale);
      canvas.height = Math.round(height * scale);
    }
    const context = canvas.getContext('2d');
    context.setTransform(scale, 0, 0, scale, 0, 0);
    context.clearRect(0, 0, width, height);
    const plotHeight = height - 16;  // the frequency labels sit under the plot
    const y = (db) => plotHeight - ((db - range.low) / Math.max(1, range.high - range.low)) * plotHeight;
    context.lineWidth = 1;
    context.font = '11px sans-serif';
    // Recessive grid: every 10 dB across, and round frequencies down.
    context.strokeStyle = css('--bar');
    context.fillStyle = css('--dim');
    for (let db = Math.ceil(range.low / 10) * 10; db <= range.high; db += 10) {
      const py = Math.round(y(db)) + 0.5;
      context.beginPath(); context.moveTo(0, py); context.lineTo(width, py); context.stroke();
      context.fillText(`${db} dB`, 3, py - 3);
    }
    if (span) {
      const x = (hz) => ((hz - span.low) / (span.high - span.low)) * width;
      const every = tickStep(width);
      context.textAlign = 'center';
      for (let hz = Math.ceil(span.low / every) * every; hz <= span.high; hz += every) {
        const px = Math.round(x(hz)) + 0.5;
        context.beginPath(); context.moveTo(px, 0); context.lineTo(px, plotHeight); context.stroke();
        context.fillText(mhz(hz), Math.min(width - 30, Math.max(30, px)), height - 3);
      }
      context.textAlign = 'left';
      // Your channel: the passband shaded, its frequency a line.
      const [lo, hi] = passband();
      context.fillStyle = css('--viz');
      context.globalAlpha = 0.18;
      context.fillRect(x(lo), 0, Math.max(2, x(hi) - x(lo)), plotHeight);
      context.globalAlpha = 1;
      context.fillRect(Math.round(x(listen.frequency_hz)), 0, 1, plotHeight);
    }
    if (smooth) {
      // The spectrum: one 2px line.
      context.strokeStyle = css('--viz');
      context.lineWidth = 2;
      context.beginPath();
      for (let i = 0; i < smooth.length; i++) {
        const px = (i / (smooth.length - 1)) * width;
        const py = Math.max(0, Math.min(plotHeight, y(smooth[i] / 2 + DB_FLOOR)));
        if (i) context.lineTo(px, py); else context.moveTo(px, py);
      }
      context.stroke();
    }
    if (hover) {
      context.fillStyle = css('--ink');
      context.fillRect(Math.round(hover.x), 0, 1, plotHeight);
    }
  }

  $effect(() => {
    range.low; range.high;  // a contrast change repaints everything kept
    if (range.low >= range.high) return;
    redrawWaterfall();
    drawSpectrum();
    remember();
  });

  $effect(() => {
    listen.frequency_hz; listen.mode; bandwidth; split; sdr.center_hz; sdr.rate;
    requestAnimationFrame(drawSpectrum);
  });

  // --- pointer: hover, click to tune, drag the channel or its edges ------------------------------
  function at(event) {
    if (!span || !plots) return null;
    const rect = plots.getBoundingClientRect();
    const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    const toPx = (hz) => ((hz - span.low) / (span.high - span.low)) * rect.width;
    return { x: event.clientX - rect.left, y: event.clientY - rect.top, hz: span.low + fraction * (span.high - span.low), fraction, toPx };
  }

  // What a press here would grab: an edge of the passband, its middle, or nothing.
  // A channel only a few pixels wide is all middle, so it can always be moved.
  function grab(point) {
    const [lo, hi] = passband();
    const [xl, xh] = [point.toPx(lo), point.toPx(hi)];
    if (xh - xl < EDGE_PX * 4) return Math.abs(point.x - (xl + xh) / 2) <= EDGE_PX ? 'move' : 'tune';
    if (Math.abs(point.x - xl) <= EDGE_PX && listen.mode !== 'usb') return 'low';
    if (Math.abs(point.x - xh) <= EDGE_PX && listen.mode !== 'lsb') return 'high';
    if (point.x > xl && point.x < xh) return 'move';
    return 'tune';
  }

  function onDown(event) {
    const point = at(event);
    if (!point || event.button !== 0) return;
    drag = { kind: grab(point), startX: point.x, startHz: point.hz, startFreq: listen.frequency_hz, moved: false };
    try {
      plots.setPointerCapture(event.pointerId);  // keep the drag going outside the plot
    } catch { /* not a real pointer, or already released: the drag still works inside */ }
  }

  function onMove(event) {
    const point = at(event);
    if (!point) return;
    if (drag) {
      if (Math.abs(point.x - drag.startX) > 3) drag.moved = true;
      if (!drag.moved) return;
      if (drag.kind === 'move' || drag.kind === 'tune') {
        const hz = drag.startFreq + (point.hz - drag.startHz);
        listen.frequency_hz = Math.round(Math.min(span.high, Math.max(span.low, hz)) / step) * step;
        drag.kind = 'move';
      } else {
        const edge = Math.abs(point.hz - listen.frequency_hz);
        const bw = listen.mode === 'usb' || listen.mode === 'lsb' ? edge : edge * 2;
        listen.bandwidth_hz = Math.round(Math.min(modeInfo[4], Math.max(modeInfo[3], bw)));
      }
      dragging = drag.kind;
      hover = null;
      sendListenSoon();
      drawSpectrum();
      return;
    }
    const bin = latest ? Math.min(latest.length - 1, Math.floor(point.fraction * latest.length)) : 0;
    hover = { x: point.x, y: point.y, hz: point.hz, db: latest ? latest[bin] / 2 + DB_FLOOR : null, grab: grab(point) };
    drawSpectrum();
  }

  function onUp(event) {
    if (!drag) return;
    const point = at(event);
    if (!drag.moved && point) tuneTo(point.hz, { snap: true });
    else sendListen();
    drag = null;
    dragging = null;
  }

  function onLeave() {
    hover = null;
    drawSpectrum();
  }

  function onWheel(event) {
    if (!listen.frequency_hz) return;
    event.preventDefault();
    tuneTo(listen.frequency_hz + (event.deltaY < 0 ? step : -step));
  }

  const cursor = $derived(dragging ? (dragging === 'move' ? 'grabbing' : 'ew-resize')
    : hover?.grab === 'low' || hover?.grab === 'high' ? 'ew-resize' : hover?.grab === 'move' ? 'grab' : 'crosshair');

  function autoRange() {
    if (!smooth) return;
    const sorted = Array.from(smooth).sort((a, b) => a - b);
    const floor = sorted[Math.floor(sorted.length * 0.2)] / 2 + DB_FLOOR;
    const peak = sorted[sorted.length - 1] / 2 + DB_FLOOR;
    range = { low: Math.round(floor - 5), high: Math.round(Math.max(floor + 20, peak + 5)) };
  }

  function sendGain() {
    send({ t: 'gain', db: gainAuto ? null : Number(gainValue) });
  }

  onMount(() => {
    if (!sourceId) {
      loadTuners();
      const timer = setInterval(loadTuners, 10000);
      return () => clearInterval(timer);
    }
    connect();
    const redraw = () => drawSpectrum();
    addEventListener('resize', redraw);
    return () => {
      removeEventListener('resize', redraw);
      clearTimeout(retry);
      clearTimeout(pending);
      fatal = 'closed';
      socket?.close();
      stopAudio();
    };
  });
</script>

{#if !sourceId}
  <h1>Tuner</h1>
  <p class="small">Tune around with an SDR from here, like GQRX: a waterfall, any frequency the stick reaches, and your own choice of mode. One person at a time owns a tuner and moves it; anyone else on it picks their own channel inside its span. An SDR becomes a tuner when it's assigned a Tuner target on the Sources page. Each opens in its own window.</p>
  {#if listError}<div class="err" role="alert">{listError}</div>{/if}
  <div class="box">
    {#each tuners ?? [] as t (t.source_id)}
      <div class="call">
        <div><strong>{t.name}</strong> <span class="small dim">on {t.node_name}</span></div>
        <div class="small">
          {#if t.error}<span class="st-bad">{t.error}</span>{:else if t.state === 'listening'}<span class="st-ok">Ready</span>{:else}{t.state || 'Starting'}{/if}
          {#if t.viewers}, {t.viewers} {t.viewers === 1 ? 'person' : 'people'} on it{t.controller ? `, owned by ${t.controller}` : ''}{/if}
        </div>
        <div class="buttons"><a class="button" href={`#/tuner?source=${t.source_id}`} target="_blank" rel="noopener">Open tuner</a></div>
      </div>
    {:else}
      {#if tuners}<p class="pad dim">No SDR is a tuner right now. Add a target of type Tuner under Targets, then assign it to an SDR on the Sources page.</p>{/if}
    {/each}
  </div>
{:else}
  <div class="tuner-app">
    <header class="tuner-top">
      <FreqReadout value={listen.frequency_hz} onchange={(hz) => tuneTo(hz)} label="Your channel" />
      <form class="tuner-goto" onsubmit={(event) => { event.preventDefault(); tuneTo(parseMHz(gotoText)); gotoText = ''; }}>
        <input class="mono" bind:value={gotoText} inputmode="decimal" placeholder="Go to MHz" aria-label="Go to a frequency, in MHz" />
      </form>
      {#if audioOn}
        <button onclick={stopAudio}>Stop audio</button>
      {:else}
        <button class="main" onclick={startAudio}>Start audio</button>
      {/if}
      <div class="tuner-status small">
        <strong>{sourceName || 'Tuner'}:</strong>
        {#if !connected}<span class="st-warn">connecting...</span>
        {:else if !sdr.running}<span class="st-warn">{sdr.error || 'starting, or not running'}</span>
        {:else if owner}you own this tuner.
        {:else if sdr.controller}{sdr.controller} owns this tuner.
        {:else}nobody owns this tuner.{/if}
        {#if sdr.viewers > 1}{sdr.viewers} people on it.{/if}
        {#if sdr.can_claim}<button onclick={() => send({ t: 'claim' })}>Take the tuner</button>{/if}
        {#if sdr.can_take}<button onclick={() => send({ t: 'claim' })}>Take it over</button>{/if}
        {#if owner}<button onclick={() => send({ t: 'release' })}>Release</button>{/if}
      </div>
      <a class="small tuner-back" href="#/tuner">All tuners</a>
    </header>

    {#if fatal && fatal !== 'closed'}
      <div class="err tuner-message" role="alert">{fatal}</div>
    {:else if error}
      <div class="err tuner-message" role="alert">{error} <button class="link-button" onclick={() => (error = '')}>Dismiss</button></div>
    {/if}

    <div class="tuner-body">
      <!-- svelte-ignore a11y_no_static_element_interactions: the readout, Go to and the panel do the same by keyboard -->
      <div class="tuner-plots" bind:this={plots} style:cursor={cursor} style:grid-template-rows={`${split}% 1fr`}
        onpointerdown={onDown} onpointermove={onMove} onpointerup={onUp} onpointerleave={onLeave} onwheel={onWheel}>
        <canvas class="tuner-spectrum" bind:this={spectrumCanvas} aria-label="Spectrum"></canvas>
        <canvas class="tuner-waterfall" bind:this={waterfallCanvas} width="1024" height={HISTORY} aria-label="Waterfall"></canvas>
        {#if hover}
          <div class="tuner-tip small mono" style:left={`${Math.min(hover.x + 12, (plots?.clientWidth ?? 0) - 160)}px`} style:top={`${hover.y + 14}px`}>
            {mhz(hover.hz)} MHz{hover.db != null ? `, ${hover.db.toFixed(0)} dB` : ''}
          </div>
        {/if}
      </div>

      <aside class="tuner-panel">
        <div class="tuner-tabs" role="tablist">
          {#each [['receiver', 'Receiver'], ...(owner ? [['input', 'Input']] : []), ['fft', 'FFT'], ['bookmarks', 'Bookmarks']] as [id, label] (id)}
            <button role="tab" aria-selected={tab === id} class:active={tab === id} onclick={() => (tab = id)}>{label}</button>
          {/each}
        </div>
        <div class="tuner-pane">
          {#if tab === 'receiver'}
            <div class="tuner-grid">
              <label for="tuner-mode">Mode</label>
              <select id="tuner-mode" value={listen.mode} onchange={(event) => setMode(event.target.value)}>
                {#each MODES as [id, label] (id)}<option value={id}>{label}</option>{/each}
              </select>
              <label for="tuner-bw">Filter</label>
              <div class="tuner-row">
                <input id="tuner-bw" class="mono" type="number" step="any" min={modeInfo[3] / 1000} max={modeInfo[4] / 1000}
                  value={bandwidth / 1000} onchange={(event) => setBandwidth(Number(event.target.value) * 1000)} />
                <span class="small">kHz</span>
              </div>
              <span></span>
              <div class="tuner-row">
                <button onclick={() => setBandwidth(modeInfo[2] / 2)}>Narrow</button>
                <button onclick={() => setBandwidth(modeInfo[2])}>Normal</button>
                <button onclick={() => setBandwidth(modeInfo[2] * 1.6)}>Wide</button>
              </div>
              <label for="tuner-step">Step</label>
              <select id="tuner-step" bind:value={step} onchange={remember}>
                {#each STEPS as s (s)}<option value={s}>{s >= 1000 ? `${s / 1000} kHz` : `${s} Hz`}</option>{/each}
              </select>
              <label for="tuner-squelch">Squelch</label>
              <div class="tuner-row">
                <input id="tuner-squelch" type="checkbox" bind:checked={squelchOn} onchange={sendListen} aria-label="Squelch on" />
                <input type="range" min="-130" max="0" step="1" bind:value={squelchLevel} disabled={!squelchOn} onchange={sendListen} aria-label="Squelch level" />
                <span class="small mono">{squelchOn ? `${squelchLevel} dB` : 'off'}</span>
              </div>
              <span></span>
              <div class="tuner-row"><button disabled={!squelchOn} onclick={() => { squelchLevel = Math.round(rx.level_db + 3); sendListen(); }}>Just above the noise now</button></div>
              <label for="tuner-volume">Volume</label>
              <div class="tuner-row">
                <input id="tuner-volume" type="range" min="0" max="2" step="0.05" bind:value={volume} onchange={remember} />
                <label class="small"><input type="checkbox" bind:checked={muted} /> Mute</label>
              </div>
              {#if listen.mode === 'wfm' || listen.mode === 'wfm_stereo'}
                <label for="tuner-deemph">De-emphasis</label>
                <select id="tuner-deemph" bind:value={listen.deemphasis_us} onchange={sendListen}>
                  <option value={75}>75 µs (the Americas)</option>
                  <option value={50}>50 µs (most elsewhere)</option>
                </select>
              {/if}
              <span>Signal</span>
              <div>
                <meter class="tuner-meter" min="-130" max="0" low="-100" high="-40" value={rx.level_db ?? -130}></meter>
                <div class="small mono">{rx.level_db?.toFixed(1)} dB{squelchOn ? (rx.open ? ', open' : ', squelched') : ''}{listen.mode === 'wfm_stereo' ? (rx.stereo ? ', stereo' : ', mono') : ''}{rx.inside ? '' : ', outside the span'}</div>
              </div>
              {#if rx.rds && (rx.rds.ps || rx.rds.rt)}
                <span>RDS</span>
                <div><strong class="mono">{rx.rds.ps}</strong> <span class="small dim mono">{rx.rds.pi}</span>{#if rx.rds.rt}<div class="small">{rx.rds.rt}</div>{/if}</div>
              {/if}
            </div>
            <p class="small dim">Click the waterfall to tune, drag the shaded channel to move it, or drag its edge to change the filter. The mouse wheel steps.</p>
          {:else if tab === 'input' && owner}
            <div class="tuner-grid">
              <label for="tuner-center">Centre</label>
              <form class="tuner-row" onsubmit={(event) => { event.preventDefault(); setCenter(parseMHz(centerText)); }}>
                <input id="tuner-center" class="mono" inputmode="decimal" bind:value={centerText} oninput={() => (centerEdited = true)} autocomplete="off" />
                <span class="small">MHz</span>
                <button class="main">Set</button>
              </form>
              <span></span>
              <div class="tuner-row"><button onclick={() => setCenter(Math.round(listen.frequency_hz - sdr.rate / 4), { moveChannel: false })}>Move the span to my channel</button></div>
              <label for="tuner-rate">Sample rate</label>
              <select id="tuner-rate" value={sdr.rate} onchange={(event) => send({ t: 'rate', hz: Number(event.target.value) })}>
                {#each RATES as r (r)}<option value={r}>{(r / 1e6).toFixed(3)} MHz</option>{/each}
              </select>
              <label for="tuner-gain">Gain</label>
              <div class="tuner-row">
                <label class="small"><input type="checkbox" bind:checked={gainAuto} onchange={sendGain} /> Auto</label>
                <input id="tuner-gain" type="range" min="0" max="50" step="0.1" bind:value={gainValue} disabled={gainAuto} onchange={sendGain} />
                <span class="small mono">{gainAuto ? 'auto' : `${Number(gainValue).toFixed(1)} dB`}</span>
              </div>
              <label for="tuner-hand">Hand over</label>
              <div class="tuner-row">
                <select id="tuner-hand" bind:value={handTo} disabled={!sdr.handover?.length}>
                  <option value="">{sdr.handover?.length ? 'Choose who...' : 'Nobody else can tune'}</option>
                  {#each sdr.handover ?? [] as v (v.id)}<option value={String(v.id)}>{v.name}</option>{/each}
                </select>
                <button disabled={!handTo} onclick={() => { send({ t: 'handover', to: Number(handTo) }); handTo = ''; }}>Hand over</button>
              </div>
            </div>
            <p class="small dim">Everyone on this SDR hears inside the span around the centre. Wider sample rates show more and cost its computer more. Gain goes to the stick's nearest step.</p>
          {:else if tab === 'fft'}
            <div class="tuner-grid">
              <label for="tuner-low">Range</label>
              <div class="tuner-row">
                <input id="tuner-low" class="mono" type="number" step="1" min="-130" max="0" bind:value={range.low} oninput={() => (rangeSet = true)} aria-label="Lowest level shown" />
                <span class="small">to</span>
                <input class="mono" type="number" step="1" min="-130" max="0" bind:value={range.high} oninput={() => (rangeSet = true)} aria-label="Highest level shown" />
                <span class="small">dB</span>
              </div>
              <span></span>
              <div class="tuner-row"><button onclick={() => { rangeSet = true; autoRange(); }}>Auto range</button></div>
              <label for="tuner-split">Spectrum</label>
              <div class="tuner-row"><input id="tuner-split" type="range" min="15" max="70" step="1" bind:value={split} onchange={remember} /><span class="small mono">{split}%</span></div>
              <label for="tuner-avg">Averaging</label>
              <div class="tuner-row"><input id="tuner-avg" type="range" min="0.05" max="1" step="0.05" bind:value={averaging} onchange={remember} /><span class="small mono">{averaging >= 1 ? 'off' : averaging.toFixed(2)}</span></div>
            </div>
            <p class="small dim">These change only what you see.</p>
          {:else}
            <TunerBookmarks current={listen} onpick={pick} />
          {/if}
        </div>
      </aside>
    </div>
  </div>
{/if}
