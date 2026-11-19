<script>
  // Back and forward through a long list. Shown above and below it, so neither end is far away.
  // Newest first, forward goes older; oldest first, forward goes newer.
  let { offset, total, size, order = 'desc', onpage } = $props();
  const back = $derived(order === 'asc' ? 'Older' : 'Newer');
  const forward = $derived(order === 'asc' ? 'Newer' : 'Older');
</script>

{#if total > size}
  <div class="pager">
    <button type="button" disabled={offset === 0} onclick={() => onpage(-size)}>{back}</button>
    <span class="small">{offset + 1} to {Math.min(offset + size, total)} of {total}</span>
    <button type="button" disabled={offset + size >= total} onclick={() => onpage(size)}>{forward}</button>
  </div>
{/if}
