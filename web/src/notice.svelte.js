// Info and warning notices clear themselves. Errors are left alone: they stay until the next action.
export const NOTICE_MS = 10000;

// Call during component setup. A new message restarts the countdown.
export function clearAfter(read, clear, ms = NOTICE_MS) {
  $effect(() => {
    if (!read()) return;
    const timer = setTimeout(clear, ms);
    return () => clearTimeout(timer);
  });
}
