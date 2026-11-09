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
</script>

{#if warnings.length}
  <div class={bad ? 'err' : 'warn'} role="status">
    {#if warnings.length === 1}
      <a class="warning-one" href={warnings[0].page}>{warnings[0].text}</a>
    {:else}
      <button type="button" class="link-button" onclick={() => (open = !open)} aria-expanded={open}>{warnings.length} things need attention</button>
    {/if}
    {#if open && warnings.length > 1}
      <ul class="warning-list">
        {#each warnings as warning, i (i)}<li><a href={warning.page}>{warning.text}</a></li>{/each}
      </ul>
    {:else if warnings.length > 1}
      <span class="small"> (show)</span>
    {/if}
    {#if admin}<a class="small warning-manage" href="#/admin?tab=warnings">Mute</a>{/if}
  </div>
{/if}
