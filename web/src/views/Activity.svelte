<script>
  // Activity over 7, 30 or 90 days. Hand-drawn SVG: one hue for every magnitude chart.
  import { onMount } from 'svelte';
  import { api, DAYS, formatDuration } from '../lib.js';

  let data = $state(null);
  let days = $state(30);
  let error = $state('');
  let loading = $state(false);
  let tip = $state(null);  // { x, y, value, label } over the chart being hovered

  async function load() {
    loading = true;
    try {
      data = await api(`stats?days=${days}`);
      error = '';
    } catch (failure) {
      error = failure.message;
    } finally {
      loading = false;
    }
  }

  onMount(load);

  function choose(next) {
    days = next;
    load();  // the old charts stay, dimmed, until the new numbers arrive
  }

  const hours = (seconds) => (seconds >= 3600 ? `${(seconds / 3600).toFixed(seconds >= 36000 ? 0 : 1)} h` : formatDuration(seconds));
  const compact = (n) => (n >= 10000 ? `${Math.round(n / 1000)}K` : n.toLocaleString());
  const dayLabel = (iso, options) => new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, options);

  // Clean axis ticks: 0 and up to four round steps.
  function ticks(max) {
    if (max <= 0) return [0];
    const raw = max / 4;
    const power = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((s) => s >= raw);
    const top = Math.ceil(max / step) * step;
    return Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step);
  }

  // --- calls per day: columns
  let width = $state(640);  // the chart is drawn at its box's real width, so text never stretches
  const W = $derived(Math.max(260, width - 2));
  const H = 180, LEFT = 36, BOTTOM = 22, TOP = 8;
  const perDay = $derived(data?.per_day ?? []);
  const dayTicks = $derived(ticks(Math.max(0, ...perDay.map((d) => d.calls))));
  const dayMax = $derived(dayTicks.at(-1) || 1);
  const band = $derived((W - LEFT) / Math.max(1, perDay.length));
  const colWidth = $derived(Math.min(24, Math.max(2, band - 2)));  // capped, with a gap between columns
  const y = (value) => TOP + (H - TOP - BOTTOM) * (1 - value / dayMax);

  // A column: square at the baseline, 4px rounded at the data end (less on very thin columns).
  function column(x, value) {
    const top = y(value), bottom = y(0), w = colWidth;
    const r = Math.min(4, w / 2, bottom - top);
    if (value <= 0) return '';
    return `M${x},${bottom} V${top + r} Q${x},${top} ${x + r},${top} H${x + w - r} Q${x + w},${top} ${x + w},${top + r} V${bottom} Z`;
  }
  const labelEvery = $derived(perDay.length > 31 ? 14 : perDay.length > 8 ? 7 : 1);

  // --- busiest hours: heatmap
  const heat = $derived(data?.by_hour ?? []);
  const heatMax = $derived(Math.max(1, ...heat.flat()));

  function show(event, label, value, chart = 'days') {
    const box = event.currentTarget.closest('.chart').getBoundingClientRect();
    const mark = event.currentTarget.getBoundingClientRect();
    const x = mark.left - box.left + mark.width / 2;
    // Near an edge the tip lines up with that edge, so it never runs off the screen.
    const align = x < 90 ? 'start' : x > box.width - 90 ? 'end' : 'middle';
    tip = { x: align === 'start' ? 0 : align === 'end' ? box.width : x, y: mark.top - box.top, label, value, align, chart };
  }
  const hide = () => (tip = null);

  const channels = $derived((data?.channels ?? []).slice(0, 10));
  const channelMax = $derived(Math.max(1, ...channels.map((c) => c.calls)));
  const sdrMax = $derived(Math.max(0.0001, ...(data?.sdrs ?? []).map((s) => s.busy)));
  const percent = (share) => (share > 0 && share < 0.001 ? '<0.1%' : `${(share * 100).toFixed(share < 0.1 ? 1 : 0)}%`);
  const labelName = { broadcastify: 'Broadcastify', clearcut: 'ClearCut', rdio: 'RDIO' };
</script>

<h1>Activity</h1>
<div class="buttons" role="group" aria-label="Period">
  {#each [7, 30, 90] as option (option)}
    <button type="button" aria-pressed={days === option} class:main={days === option} onclick={() => choose(option)}>Last {option} days</button>
  {/each}
</div>
{#if error}<div class="err" role="alert">{error}</div>{/if}

{#if data}
  <div class="viz" class:refreshing={loading}>
    <div class="tiles">
      <div class="tile"><div class="small dim">Calls</div><div class="tile-value">{compact(data.totals.calls)}</div></div>
      <div class="tile"><div class="small dim">Airtime</div><div class="tile-value">{hours(data.totals.seconds)}</div></div>
      <div class="tile"><div class="small dim">Calls today</div><div class="tile-value">{compact(data.totals.today)}</div></div>
      <div class="tile"><div class="small dim">ACARS messages</div><div class="tile-value">{compact(data.totals.messages)}</div></div>
    </div>

    <div class="box">
      <h2>Calls per day</h2>
      <div class="pad chart">
        <div bind:clientWidth={width}>
        <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img" aria-label={`Calls per day over the last ${days} days`} class="columns">
          {#each dayTicks as tick (tick)}
            <line x1={LEFT} x2={W} y1={y(tick)} y2={y(tick)} class="grid" />
            <text x={LEFT - 6} y={y(tick) + 4} text-anchor="end" class="axis">{compact(tick)}</text>
          {/each}
          {#each perDay as d, i (d.day)}
            {@const x = LEFT + i * band + (band - colWidth) / 2}
            <path d={column(x, d.calls)} class="mark" />
            <!-- The hit target is the whole band, not the column. -->
            <rect x={LEFT + i * band} y={TOP} width={band} height={H - TOP - BOTTOM} class="hit" tabindex="0" role="img"
              aria-label={`${dayLabel(d.day, { weekday: 'short', month: 'short', day: 'numeric' })}: ${d.calls} calls`}
              onpointerenter={(e) => show(e, dayLabel(d.day, { weekday: 'short', month: 'short', day: 'numeric' }), `${d.calls} call${d.calls === 1 ? '' : 's'}, ${hours(d.seconds)}`)}
              onfocus={(e) => show(e, dayLabel(d.day, { weekday: 'short', month: 'short', day: 'numeric' }), `${d.calls} call${d.calls === 1 ? '' : 's'}, ${hours(d.seconds)}`)}
              onpointerleave={hide} onblur={hide} />
            {#if (i % labelEvery === 0 && perDay.length - 1 - i >= Math.ceil(labelEvery / 2)) || i === perDay.length - 1}
              <text x={LEFT + i * band + band / 2} y={H - 6} text-anchor="middle" class="axis">{dayLabel(d.day, { month: 'short', day: 'numeric' })}</text>
            {/if}
          {/each}
        </svg>
        </div>
        {#if tip?.chart === 'days'}<div class={`tip tip-${tip.align}`} style={`left:${tip.x}px;top:${tip.y}px`}><strong>{tip.value}</strong><br /><span class="small">{tip.label}</span></div>{/if}
        <details><summary class="small">Show as a table</summary>
          <table class="data"><thead><tr><th>Day</th><th>Calls</th><th>Airtime</th></tr></thead>
            <tbody>{#each perDay as d (d.day)}<tr><td>{dayLabel(d.day, { weekday: 'short', month: 'short', day: 'numeric' })}</td><td>{d.calls}</td><td>{hours(d.seconds)}</td></tr>{/each}</tbody>
          </table>
        </details>
      </div>
    </div>

    <div class="box">
      <h2>Busiest hours</h2>
      <div class="pad chart">
        <p class="small dim">Calls by day of the week and hour, in {data.timezone}. Darker means more.</p>
        <div class="heat" role="img" aria-label="Calls by day of the week and hour">
          <span></span>
          {#each Array(24) as _, h (h)}<span class="axis-h">{h % 3 === 0 ? h : ''}</span>{/each}
          {#each heat as row, d (d)}
            <span class="axis-d">{DAYS[d]}</span>
            {#each row as count, h (h)}
              <span class="cell" tabindex="0" role="img" aria-label={`${DAYS[d]} ${h}:00: ${count} calls`}
                style={`--level:${count ? 0.12 + 0.88 * count / heatMax : 0}`}
                onpointerenter={(e) => show(e, `${DAYS[d]}, ${String(h).padStart(2, '0')}:00 to ${String(h + 1).padStart(2, '0')}:00`, `${count} call${count === 1 ? '' : 's'}`, 'heat')}
                onfocus={(e) => show(e, `${DAYS[d]}, ${String(h).padStart(2, '0')}:00`, `${count} call${count === 1 ? '' : 's'}`, 'heat')}
                onpointerleave={hide} onblur={hide}></span>
            {/each}
          {/each}
        </div>
        {#if tip?.chart === 'heat'}<div class={`tip tip-${tip.align}`} style={`left:${tip.x}px;top:${tip.y}px`}><strong>{tip.value}</strong><br /><span class="small">{tip.label}</span></div>{/if}
        <div class="heat-key small dim">Fewer <span class="ramp"></span> More (up to {heatMax} in an hour)</div>
        <details><summary class="small">Show as a table</summary>
          <div class="table-scroll"><table class="data"><thead><tr><th>Hour</th>{#each DAYS as day (day)}<th>{day}</th>{/each}</tr></thead>
            <tbody>{#each Array(24) as _, h (h)}<tr><td>{String(h).padStart(2, '0')}:00</td>{#each heat as row, d (d)}<td>{row[h]}</td>{/each}</tr>{/each}</tbody>
          </table></div>
        </details>
      </div>
    </div>

    <div class="box">
      <h2>Busiest channels</h2>
      <div class="pad">
        {#each channels as c (c.name)}
          <div class="rank">
            <div class="rank-name">{c.name}</div>
            <div class="rank-track"><div class="rank-bar" style={`--share:${c.calls / channelMax}`}></div><span class="rank-value">{c.calls} · {hours(c.seconds)}</span></div>
          </div>
        {:else}
          <p class="small dim">No calls in this period.</p>
        {/each}
        {#if (data.channels ?? []).length > 10}<p class="small dim">Showing the top 10 of {data.channels.length}.</p>{/if}
      </div>
    </div>

    <div class="box">
      <h2>How busy each SDR was</h2>
      <div class="pad">
        <p class="small dim">The share of the period each SDR spent in a call.</p>
        {#each data.sdrs as s (s.name)}
          <div class="rank">
            <div class="rank-name">{s.name}{s.current ? '' : ' (removed)'}</div>
            <div class="rank-track"><div class="rank-bar" style={`--share:${s.busy / sdrMax}`}></div><span class="rank-value">{percent(s.busy)} · {hours(s.seconds)}</span></div>
          </div>
        {:else}
          <p class="small dim">No calls in this period.</p>
        {/each}
      </div>
    </div>

    {#if data.uploads}
      <div class="box">
        <h2>Uploads</h2>
        <div class="pad">
          {#if data.uploads.length}
            <div class="table-scroll"><table class="data">
              <thead><tr><th>Service</th><th>Sent</th><th>Skipped</th><th>Failed</th><th>Waiting</th><th>Success</th></tr></thead>
              <tbody>
                {#each data.uploads as u (u.destination)}
                  {@const finished = u.sent + u.failed}
                  <tr>
                    <td>{labelName[u.destination] ?? u.destination}</td><td>{u.sent}</td><td>{u.skipped}</td>
                    <td class:st-bad={u.failed}>{u.failed}</td><td>{u.waiting}</td>
                    <td><span class="meter" role="img" aria-label={finished ? `${percent(u.sent / finished)} sent` : 'none finished'}><span style={`width:${finished ? (u.sent / finished) * 100 : 0}%`}></span></span> {finished ? percent(u.sent / finished) : '—'}</td>
                  </tr>
                {/each}
              </tbody>
            </table></div>
            <p class="small dim">Skipped calls were duplicates or already on the service. Failed ones show their reason on the recording.</p>
          {:else}
            <p class="small dim">No uploads in this period.</p>
          {/if}
        </div>
      </div>
    {/if}
  </div>
{/if}
