<script>
  // A gain picker. With the stick's steps known it offers only those, plus AGC;
  // otherwise it falls back to a number box. Empty ('') means AGC.
  import { nearestStep } from './lib.js';

  let { steps = [], value = $bindable(''), id = undefined } = $props();

  const isSet = (v) => v !== '' && v !== null && v !== undefined;
  // A saved gain from before the steps were known stays selectable until changed.
  const legacy = $derived(isSet(value) && steps.length && !steps.some((s) => Math.abs(s - Number(value)) <= 0.05) ? Number(value) : null);
</script>

{#if steps.length}
  <select {id} class="mono" bind:value>
    <option value="">AGC (automatic)</option>
    {#if legacy !== null}<option value={legacy}>{legacy} dB (stick uses {nearestStep(steps, legacy)})</option>{/if}
    {#each steps as step (step)}<option value={step}>{step} dB</option>{/each}
  </select>
{:else}
  <input {id} class="mono" type="number" inputmode="decimal" step="0.1" min="0" max="60" placeholder="AGC" bind:value />
{/if}
