<script>
  // A frequency shown digit by digit, as in GQRX. Over a digit, the mouse wheel or the up and down
  // keys step it, and clicking its top or bottom half does too. Typing a number replaces the digit
  // under focus and moves to the next. Leading zeros are dimmed.
  let { value, onchange, min = 500_000, max = 2_200_000_000, label = 'Frequency' } = $props();
  const DIGITS = 10;  // up to 9.999 999 999 GHz
  let refs = [];

  const digits = $derived(String(Math.max(0, Math.round(value || 0))).padStart(DIGITS, '0').split('').map(Number));
  const leading = $derived(digits.findIndex((d) => d !== 0));
  const power = (i) => 10 ** (DIGITS - 1 - i);

  function set(hz) {
    const clamped = Math.min(max, Math.max(min, hz));
    if (clamped !== value) onchange(clamped);
  }

  function step(i, direction) {
    set(value + direction * power(i));
  }

  function onKey(event, i) {
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      event.preventDefault();
      step(i, event.key === 'ArrowUp' ? 1 : -1);
    } else if (event.key === 'ArrowLeft' && i > 0) {
      event.preventDefault();
      refs[i - 1]?.focus();
    } else if (event.key === 'ArrowRight' && i < DIGITS - 1) {
      event.preventDefault();
      refs[i + 1]?.focus();
    } else if (/^[0-9]$/.test(event.key)) {
      event.preventDefault();
      set(value + (Number(event.key) - digits[i]) * power(i));
      refs[Math.min(DIGITS - 1, i + 1)]?.focus();
    }
  }

  function onWheel(event, i) {
    event.preventDefault();
    step(i, event.deltaY < 0 ? 1 : -1);
  }

  function onClick(event, i) {
    const rect = event.currentTarget.getBoundingClientRect();
    step(i, event.clientY < rect.top + rect.height / 2 ? 1 : -1);
  }
</script>

<div class="readout mono" role="group" aria-label={`${label}: ${(value / 1e6).toFixed(6)} MHz`}>
  {#each digits as digit, i (i)}
    {#if i === 1 || i === 4 || i === 7}<span class="readout-dot" aria-hidden="true">.</span>{/if}
    <span bind:this={refs[i]} class="readout-digit" class:dim={leading === -1 || i < leading} tabindex="0" role="button"
      aria-label={`Digit worth ${power(i).toLocaleString()} Hz: ${digit}. Up and down keys change it.`}
      onkeydown={(event) => onKey(event, i)} onwheel={(event) => onWheel(event, i)} onclick={(event) => onClick(event, i)}>{digit}</span>
  {/each}
  <span class="readout-unit">Hz</span>
</div>
