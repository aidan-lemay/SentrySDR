<script>
  // An info or warning notice. Clicking or tapping anywhere on it dismisses it.
  let { kind = 'msg', ondismiss, children } = $props();
</script>

<div role="status">
  <button type="button" class="{kind} notice" title="Dismiss" onclick={ondismiss}>
    <span>{@render children()}</span>
    <span class="notice-x" aria-hidden="true">×</span>
  </button>
</div>
