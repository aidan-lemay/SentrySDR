<script>
  // The bar for a CallPlayer: what's playing, what's waiting, and Skip, Hold and Stop.
  import { formatDuration, formatTime } from './lib.js';

  let { player } = $props();
  const waiting = $derived(player.held === null ? player.queue.length : player.queue.filter((c) => c.target_id === player.held).length);
</script>

{#if player.on}
  <div class="now-playing" role="status">
    {#if player.current}
      <div>
        <strong>{player.current.target_name || 'Unknown target'}</strong>
        <span class="small">{formatTime(player.current.started_at)}, {formatDuration(player.current.duration)}</span>
      </div>
      {#if player.current.transcript}<div class="small transcript">{player.current.transcript}</div>{/if}
    {:else}
      <div class="small">Waiting for the next call{player.held !== null ? ' on the held channel' : ''}...</div>
    {/if}
    <div class="small dim">{waiting} waiting{player.held !== null ? `, holding ${player.current?.target_name ?? 'this channel'}` : ''}</div>
    {#if player.failed}<div class="small st-warn">{player.failed} <button type="button" onclick={() => player.resume()}>Play</button></div>{/if}
    <div class="buttons">
      <button type="button" onclick={() => player.skip()} disabled={!player.current}>Skip</button>
      <button type="button" onclick={() => player.hold()} disabled={!player.current && player.held === null} aria-pressed={player.held !== null}>{player.held !== null ? 'Release hold' : 'Hold'}</button>
      <button type="button" class="bad" onclick={() => player.stop()}>Stop</button>
    </div>
  </div>
{/if}
