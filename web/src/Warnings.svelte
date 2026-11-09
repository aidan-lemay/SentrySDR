<script>
  // Machine problems, for admins and operators: a slave offline, an SDR fault, failing uploads,
  // a clock out of sync, a disk filling. It's the current state, so it doesn't dismiss itself.
  import { onMount } from 'svelte';
  import { api } from './lib.js';

  let { admin = false } = $props();

  let warnings = $state([]);
  let open = $state(false);

  async function load() {
    if (document.hidden) return;
    try {
      warnings = await api('warnings');
    } catch {
      // A failed check isn't itself worth a warning; the next one will try again.
    }
  }

  onMount(() => {
    load();
    const timer = setInterval(load, 30000);
    addEventListener('warnings-changed', load);  // a mute or a node set quiet, from the Admin page
    return () => {
      clearInterval(timer);
      removeEventListener('warnings-changed', load);
    };
  });

  const bad = $derived(warnings.some((w) => w.level === 'bad'));

  // Failed uploads and the like: noted, so only new ones warn. For everyone who sees warnings.
  async function acknowledge(warning) {
    try {
      await api('warnings/acknowledge', { method: 'POST', body: { key: warning.key } });
    } catch {
      // It may have cleared already; the reload shows what's left.
    }
    await load();
    dispatchEvent(new Event('warnings-changed'));
  }
</script>

{#if warnings.length}
  <div class={bad ? 'err' : 'warn'} role="status">
    {#if warnings.length === 1}
      <a class="warning-one" href={warnings[0].page}>{warnings[0].text}</a>
      {#if warnings[0].acknowledge}<button type="button" class="warning-ack" onclick={() => acknowledge(warnings[0])}>Acknowledge</button>{/if}
    {:else}
      <button type="button" class="link-button" onclick={() => (open = !open)} aria-expanded={open}>{warnings.length} things need attention</button>
    {/if}
    {#if open && warnings.length > 1}
      <ul class="warning-list">
        {#each warnings as warning (warning.key)}
          <li><a href={warning.page}>{warning.text}</a>{#if warning.acknowledge}{' '}<button type="button" class="warning-ack" onclick={() => acknowledge(warning)}>Acknowledge</button>{/if}</li>
        {/each}
      </ul>
    {:else if warnings.length > 1}
      <span class="small"> (show)</span>
    {/if}
    {#if admin}<a class="small warning-manage" href="#/admin?tab=warnings">Mute</a>{/if}
  </div>
{/if}
