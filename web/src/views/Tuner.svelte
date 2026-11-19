<script>
  // The web tuner. Without ?source= it lists the SDRs that are tuners now; with it, it's one tuner:
  // spectrum and waterfall from the server, your own channel's audio, and your bookmarks.
  import { onMount } from 'svelte';
  import { api, formatMHz } from '../lib.js';
  import TunerBookmarks from '../TunerBookmarks.svelte';

  let { route, user } = $props();
  const sourceId = $derived(Number(new URLSearchParams(route.split('?')[1] ?? '').get('source')) || null);

  // Keep in step with sentrysdr/tuner.py.
  const MODES = [
    ['nfm', 'NFM', 12500, 2500, 25000], ['am', 'AM', 10000, 2000, 20000], ['wfm', 'WFM mono', 180000, 50000, 220000],
    ['wfm_stereo', 'WFM stereo', 180000, 120000, 220000], ['usb', 'USB', 2700, 500, 5000], ['lsb', 'LSB', 2700, 500, 5000],
    ['cw', 'CW', 500, 100, 2000],
  ];
  const RATES = [250000, 1024000, 1536000, 1800000, 2048000, 2400000];
  const STEPS = [100, 1000, 5000, 6250, 10000, 12500, 25000, 100000];
  const DB_FLOOR = -130;
  const HISTORY = 240;  // waterfall rows kept, so a contrast change redraws what's there

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
  let listen = $state({ frequency_hz: 0, mode: saved.mode ?? 'nfm', bandwidth_hz: null, squelch_db: null, deemphasis_us: saved.deemphasis_us ?? 75 });
  let step = $state(saved.step ?? 12500);
  let volume = $state(saved.volume ?? 0.8);
  let muted = $state(false);
  let range = $state({ low: saved.low ?? -110, high: saved.high ?? -50 });
  // A range you set is kept for next time. Until then, the first rows choose one, and that isn't kept.
  let rangeChosen = saved.low !== undefined;
  let rangeSet = rangeChosen;
  let squelchOn = $state(false);
  let squelchLevel = $state(-80);

  let sdr = $state({ running: false });
  let rx = $state({ level_db: DB_FLOOR, open: false, inside: true, stereo: false, rds: null });
  let error = $state('');
  let fatal = $state('');
  let sourceName = $state('');
  let connected = $state(false);
  let audioOn = $state(false);
  let centerText = $state('');
  let centerEdited = false;  // typed but not set yet: updates from the server leave it alone
  let listenText = $state('');
  let hover = $state(null);  // { hz, db } under the pointer
  let gainAuto = $state(true);
  let gainValue = $state(30);

  let socket = null;
  let retry = null;
  let audio = null;  // { context, gain, next }
  let spectrumCanvas = $state();
  let waterfallCanvas = $state();
  let rows = [];  // [{ center, rate, data }], newest first
  let latest = null;
  let smooth = null;

  const span = $derived(sdr.rate ? { low: sdr.center_hz - sdr.rate / 2, high: sdr.center_hz + sdr.rate / 2 } : null);
  const modeInfo = $derived(MODES.find(([id]) => id === listen.mode) ?? MODES[0]);
  const bandwidth = $derived(listen.bandwidth_hz ?? modeInfo[2]);
  const ticks = $derived(span ? [0, 0.25, 0.5, 0.75, 1].map((f) => ({ at: f, hz: span.low + f * (span.high - span.low) })) : []);

  function remember() {
    try {
      localStorage.setItem('sentrysdr-tuner', JSON.stringify({ mode: listen.mode, deemphasis_us: listen.deemphasis_us, step, volume,
        ...(rangeSet ? { low: range.low, high: range.high } : {}) }));
      localStorage.setItem(`sentrysdr-tuner-${sourceId}`, String(listen.frequency_hz));
    } catch { /* private window: not remembered */ }
  }

  const send = (message) => socket?.readyState === 1 && socket.send(JSON.stringify(message));
  const mhz = (hz) => (hz / 1e6).toFixed(hz % 1000 ? 4 : 3);

  function parseMHz(text) {
    const value = Number(String(text).trim().replace(',', '.'));
    return Number.isFinite(value) && value > 0 ? Math.round(value * 1e6) : null;
  }

  function sendListen() {
    if (!listen.frequency_hz) return;
    send({ t: 'listen', ...listen, squelch_db: squelchOn ? squelchLevel : null });
    listenText = mhz(listen.frequency_hz);
    remember();
  }

  function tuneTo(hz, { snap = true } = {}) {
    if (!hz) return;
    listen.frequency_hz = snap ? Math.round(hz / step) * step : Math.round(hz);
    sendListen();
  }

  function setMode(mode) {
    listen.mode = mode;
    listen.bandwidth_hz = null;  // each mode starts at its own usual width
    sendListen();
  }

  function setCenter(hz) {
    if (!sdr.you_control) return;
    if (!hz) {
      error = 'Type a centre frequency in MHz, like 146.52.';
      return;
    }
    centerEdited = false;
    send({ t: 'center', hz });
  }

  // A frequency outside the span: whoever's tuning can bring it in, with the signal off the centre spike.
  function goTo(hz, mode, bw) {
    if (mode) listen.mode = mode;
    listen.bandwidth_hz = bw ?? null;
    if (span && (hz < span.low || hz > span.high)) {
      if (!sdr.you_control) {
        error = `${mhz(hz)} MHz is outside this SDR's span, and ${sdr.controller} is tuning it. You can ask them to move it.`;
        return;
      }
      send({ t: 'center', hz: Math.round(hz - sdr.rate / 4) });
    }
    tuneTo(hz, { snap: false });
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
      if (message.center_hz && !centerEdited) centerText = mhz(message.center_hz);
      if (message.gain_db === null) gainAuto = true;
      else if (message.gain_db !== undefined) { gainAuto = false; gainValue = message.gain_db; }
      if (first) {
        // Back where you were last time, if it's still in the span; otherwise the middle.
        const last = Number(localStorage.getItem(`sentrysdr-tuner-${sourceId}`));
        const low = message.center_hz - message.rate / 2, high = message.center_hz + message.rate / 2;
        tuneTo(last >= low && last <= high ? last : message.center_hz, { snap: false });
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
      smooth = smooth && smooth.length === data.length ? smooth.map((v, i) => v * 0.6 + data[i] * 0.4) : Float32Array.from(data);
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

  function drawSpectrum() {
    const canvas = spectrumCanvas;
    if (!canvas || !smooth) return;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    const scale = devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * scale)) {
      canvas.width = Math.round(width * scale);
      canvas.height = Math.round(height * scale);
    }
    const context = canvas.getContext('2d');
    context.setTransform(scale, 0, 0, scale, 0, 0);
    context.clearRect(0, 0, width, height);
    const y = (db) => height - ((db - range.low) / Math.max(1, range.high - range.low)) * height;
    // Recessive grid every 10 dB.
    context.strokeStyle = css('--bar');
    context.lineWidth = 1;
    context.fillStyle = css('--dim');
    context.font = '11px sans-serif';
    for (let db = Math.ceil(range.low / 10) * 10; db <= range.high; db += 10) {
      context.beginPath();
      context.moveTo(0, Math.round(y(db)) + 0.5);
      context.lineTo(width, Math.round(y(db)) + 0.5);
      context.stroke();
      context.fillText(`${db}`, 2, y(db) - 2);
    }
    // Your channel.
    if (span) {
      const x = (hz) => ((hz - span.low) / (span.high - span.low)) * width;
      const [lo, hi] = listen.mode === 'usb' ? [listen.frequency_hz, listen.frequency_hz + bandwidth]
        : listen.mode === 'lsb' ? [listen.frequency_hz - bandwidth, listen.frequency_hz]
          : [listen.frequency_hz - bandwidth / 2, listen.frequency_hz + bandwidth / 2];
      context.fillStyle = css('--viz');
      context.globalAlpha = 0.18;
      context.fillRect(x(lo), 0, Math.max(2, x(hi) - x(lo)), height);
      context.globalAlpha = 1;
      context.fillRect(Math.round(x(listen.frequency_hz)), 0, 1, height);
    }
    // The spectrum: one 2px line.
    context.strokeStyle = css('--viz');
    context.lineWidth = 2;
    context.beginPath();
    for (let i = 0; i < smooth.length; i++) {
      const px = (i / (smooth.length - 1)) * width;
      const py = Math.max(0, Math.min(height, y(smooth[i] / 2 + DB_FLOOR)));
      if (i) context.lineTo(px, py); else context.moveTo(px, py);
    }
    context.stroke();
    if (hover) {
      context.fillStyle = css('--ink');
      context.fillRect(Math.round(hover.x), 0, 1, height);
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
    listen.frequency_hz; listen.mode; bandwidth;
    drawSpectrum();
  });

  function pointer(event) {
    if (!span) return null;
    const rect = event.currentTarget.getBoundingClientRect();
    const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    return { x: event.clientX - rect.left, hz: span.low + fraction * (span.high - span.low), fraction };
  }

  function onMove(event) {
    const at = pointer(event);
    if (!at || !latest) return;
    const bin = Math.min(latest.length - 1, Math.floor(at.fraction * latest.length));
    hover = { ...at, db: latest[bin] / 2 + DB_FLOOR };
    drawSpectrum();
  }

  function onLeave() {
    hover = null;
    drawSpectrum();
  }

  function onPick(event) {
    const at = pointer(event);
    if (at) tuneTo(at.hz);
  }

  function onWheel(event) {
    if (!listen.frequency_hz) return;
    event.preventDefault();
    tuneTo(listen.frequency_hz + (event.deltaY < 0 ? step : -step));
  }

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
      fatal = 'closed';
      socket?.close();
      stopAudio();
    };
  });
</script>

{#if !sourceId}
  <h1>Tuner</h1>
  <p class="small">Tune around with an SDR from here, like GQRX: a waterfall, any frequency the stick reaches, and your own choice of mode. An SDR becomes a tuner when it's assigned a Tuner target on the Sources page. Each opens in its own tab.</p>
  {#if listError}<div class="err" role="alert">{listError}</div>{/if}
  <div class="box">
    {#each tuners ?? [] as t (t.source_id)}
      <div class="call">
        <div><strong>{t.name}</strong> <span class="small dim">on {t.node_name}</span></div>
        <div class="small">
          {#if t.error}<span class="st-bad">{t.error}</span>{:else if t.state === 'listening'}<span class="st-ok">Ready</span>{:else}{t.state || 'Starting'}{/if}
          {#if t.viewers}, {t.viewers} {t.viewers === 1 ? 'person' : 'people'} on it{t.controller ? `, ${t.controller} tuning` : ''}{/if}
        </div>
        <div class="buttons"><a class="button" href={`#/tuner?source=${t.source_id}`} target="_blank" rel="noopener">Open tuner</a></div>
      </div>
    {:else}
      {#if tuners}<p class="pad dim">No SDR is a tuner right now. Add a target of type Tuner under Targets, then assign it to an SDR on the Sources page.</p>{/if}
    {/each}
  </div>
{:else}
  <h1>{sourceName || 'Tuner'}</h1>
  {#if fatal && fatal !== 'closed'}
    <div class="err" role="alert">{fatal}</div>
    <p class="small"><a href="#/tuner">All tuners</a></p>
  {:else}
    {#if error}<div class="err" role="alert">{error} <button class="link-button" onclick={() => (error = '')}>Dismiss</button></div>{/if}
    <p class="small">
      {#if !connected}<span class="st-warn">Connecting...</span>
      {:else if !sdr.running}<span class="st-warn">{sdr.error || 'The SDR is starting, or isn\'t running.'}</span>
      {:else if sdr.you_control}You're tuning this SDR.{#if sdr.viewers > 1} {sdr.viewers - 1} other{sdr.viewers > 2 ? 's are' : ' is'} listening.{/if}
      {:else}{sdr.controller} is tuning this SDR. You choose your own frequency inside its span.
        {#if sdr.can_take}<button class="link-button" onclick={() => send({ t: 'take' })}>Take control</button>{/if}
      {/if}
    </p>

    <div class="buttons">
      {#if audioOn}
        <button onclick={stopAudio}>Stop audio</button>
        <label class="tick small"><input type="checkbox" bind:checked={muted} /> Mute</label>
      {:else}
        <button class="main" onclick={startAudio}>Start audio</button>
      {/if}
      <label class="small tuner-volume">Volume <input type="range" min="0" max="2" step="0.05" bind:value={volume} onchange={remember} aria-label="Volume" /></label>
    </div>

    <div class="box tuner-view">
      <div class="tuner-readout small mono" aria-live="off">
        {#if hover}{mhz(hover.hz)} MHz, {hover.db.toFixed(0)} dB{:else if span}{mhz(span.low)} to {mhz(span.high)} MHz{/if}
      </div>
      <!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events: the frequency fields below do the same by keyboard -->
      <div class="tuner-plots" onmousemove={onMove} onmouseleave={onLeave} onclick={onPick} onwheel={onWheel}>
        <canvas class="tuner-spectrum" bind:this={spectrumCanvas} aria-label="Spectrum"></canvas>
        <canvas class="tuner-waterfall" bind:this={waterfallCanvas} width="1024" height={HISTORY} aria-label="Waterfall"></canvas>
      </div>
      <div class="tuner-axis small mono" aria-hidden="true">
        {#each ticks as tick (tick.at)}<span style:left={`${tick.at * 100}%`} class:end={tick.at === 1} class:start={tick.at === 0} class:quarter={tick.at === 0.25 || tick.at === 0.75}>{mhz(tick.hz)}</span>{/each}
      </div>
    </div>
    <p class="small dim">Tap or click the waterfall to tune there, in steps of {formatMHz(step).replace(' MHz', '')} MHz; a mouse wheel steps up and down.</p>

    <fieldset>
      <legend>Your channel</legend>
      <form class="field" onsubmit={(event) => { event.preventDefault(); goTo(parseMHz(listenText) ?? 0); }}>
        <span>Frequency (MHz)</span>
        <div class="pair">
          <input id="tuner-listen" class="mono" inputmode="decimal" bind:value={listenText} autocomplete="off" />
          <button class="main">Tune</button>
        </div>
        {#if !rx.inside}<span class="hint st-warn">Outside this SDR's span, so silent.</span>{/if}
      </form>
      <label class="field"><span>Mode</span>
        <select value={listen.mode} onchange={(event) => setMode(event.target.value)}>
          {#each MODES as [id, label] (id)}<option value={id}>{label}</option>{/each}
        </select>
      </label>
      <label class="field"><span>Bandwidth (kHz)</span>
        <input class="mono" type="number" inputmode="decimal" step="any" min={modeInfo[3] / 1000} max={modeInfo[4] / 1000}
          value={bandwidth / 1000} onchange={(event) => { listen.bandwidth_hz = Math.round(Number(event.target.value) * 1000) || null; sendListen(); }} />
        <span class="hint">{modeInfo[3] / 1000} to {modeInfo[4] / 1000} kHz.</span>
      </label>
      <label class="field"><span>Step</span>
        <select bind:value={step} onchange={remember}>
          {#each STEPS as s (s)}<option value={s}>{s >= 1000 ? `${s / 1000} kHz` : `${s} Hz`}</option>{/each}
        </select>
      </label>
      <div class="field"><span>Squelch</span>
        <label class="tick small"><input type="checkbox" bind:checked={squelchOn} onchange={sendListen} /> Mute below</label>
        {#if squelchOn}
          <input type="range" min="-130" max="0" step="1" bind:value={squelchLevel} onchange={sendListen} aria-label="Squelch level" />
          <span class="hint">{squelchLevel} dB. The signal is {rx.level_db?.toFixed(0)} dB now.</span>
        {/if}
      </div>
      {#if listen.mode === 'wfm' || listen.mode === 'wfm_stereo'}
        <label class="field"><span>De-emphasis</span>
          <select bind:value={listen.deemphasis_us} onchange={sendListen}>
            <option value={75}>75 µs (the Americas, Korea)</option>
            <option value={50}>50 µs (Europe, most of the world)</option>
          </select>
        </label>
      {/if}
      <div class="field"><span>Signal</span>
        <meter class="tuner-meter" min="-130" max="0" low="-100" high="-40" value={rx.level_db ?? -130}></meter>
        <span class="hint mono">{rx.level_db?.toFixed(1)} dB{squelchOn ? (rx.open ? ', open' : ', squelched') : ''}{listen.mode === 'wfm_stereo' ? (rx.stereo ? ', stereo' : ', mono (no pilot)') : ''}</span>
      </div>
      {#if rx.rds && (rx.rds.ps || rx.rds.rt)}
        <div class="field"><span>RDS</span>
          <div><strong class="mono">{rx.rds.ps}</strong> <span class="small dim mono">{rx.rds.pi}</span></div>
          {#if rx.rds.rt}<div class="small">{rx.rds.rt}</div>{/if}
        </div>
      {/if}
    </fieldset>

    <fieldset>
      <legend>The SDR</legend>
      <form class="field" onsubmit={(event) => { event.preventDefault(); setCenter(parseMHz(centerText)); }}>
        <span>Centre frequency (MHz)</span>
        <div class="pair">
          <input id="tuner-center" class="mono" inputmode="decimal" bind:value={centerText} oninput={() => (centerEdited = true)} autocomplete="off" disabled={!sdr.you_control} />
          <button class="main" disabled={!sdr.you_control}>Set</button>
        </div>
        <span class="hint">Any frequency the stick can tune. Everyone on this SDR hears inside the span around it.</span>
      </form>
      {#if sdr.you_control}
        <div class="buttons form-buttons"><button type="button" onclick={() => setCenter(Math.round(listen.frequency_hz - sdr.rate / 4))}>Move the span to my channel</button></div>
      {/if}
      <label class="field"><span>Sample rate (span)</span>
        <select value={sdr.rate} disabled={!sdr.you_control} onchange={(event) => send({ t: 'rate', hz: Number(event.target.value) })}>
          {#each RATES as r (r)}<option value={r}>{(r / 1e6).toFixed(3)} MHz</option>{/each}
        </select>
        <span class="hint">Wider shows more, and costs the SDR's computer more.</span>
      </label>
      <div class="field"><span>Gain</span>
        <label class="tick small"><input type="checkbox" bind:checked={gainAuto} disabled={!sdr.you_control} onchange={sendGain} /> Automatic</label>
        {#if !gainAuto}
          <input type="range" min="0" max="50" step="0.1" bind:value={gainValue} disabled={!sdr.you_control} onchange={sendGain} aria-label="Gain" />
          <span class="hint">{Number(gainValue).toFixed(1)} dB. The stick uses its nearest step.</span>
        {/if}
      </div>
      <div class="field"><span>Waterfall range (dB)</span>
        <div class="pair">
          <input class="mono" type="number" step="1" min="-130" max="0" bind:value={range.low} oninput={() => (rangeSet = true)} aria-label="Lowest level shown" />
          <input class="mono" type="number" step="1" min="-130" max="0" bind:value={range.high} oninput={() => (rangeSet = true)} aria-label="Highest level shown" />
          <button type="button" onclick={() => { rangeSet = true; autoRange(); }}>Auto</button>
        </div>
        <span class="hint">Only changes what you see.</span>
      </div>
    </fieldset>

    <TunerBookmarks current={listen} onpick={(b) => goTo(b.frequency_hz, b.mode, b.bandwidth_hz)} />
  {/if}
{/if}
