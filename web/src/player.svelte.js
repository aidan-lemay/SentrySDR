// Plays finished calls one after another, as they arrive. Shared by the Recordings page,
// the Sources page's call lists, and Scanner. One audio element, so calls never overlap.
const MAX_QUEUE = 200;

export class CallPlayer {
  on = $state(false);
  current = $state(null);
  queue = $state([]);
  held = $state(null);  // a target id: while set, only that channel plays
  played = $state([]);  // the last few, newest first
  failed = $state('');

  #audio = null;
  #seen = new Set();
  #since = 0;

  // Turning it on starts from now: only calls that finish from here on play.
  start() {
    this.on = true;
    this.#since = Date.now() / 1000;
    this.failed = '';
    this.#audio ??= new Audio();
    this.#audio.onended = () => this.#next();
    this.#audio.onerror = () => { this.failed = 'A call couldn\'t be played; skipped it.'; this.#next(); };
  }

  stop() {
    this.on = false;
    this.queue = [];
    this.current = null;
    this.held = null;
    if (this.#audio) {
      this.#audio.pause();
      this.#audio.removeAttribute('src');
    }
  }

  toggle() {
    if (this.on) this.stop();
    else this.start();
  }

  get since() {
    return this.#since;
  }

  // Calls from a list or a poll. Only new ones that finished after start() are queued, oldest first.
  offer(calls) {
    if (!this.on) return;
    const fresh = calls
      .filter((call) => call.ended_at > this.#since && !this.#seen.has(call.id))
      .sort((a, b) => a.ended_at - b.ended_at);
    for (const call of fresh) this.#seen.add(call.id);
    if (!fresh.length) return;
    this.queue = [...this.queue, ...fresh].slice(-MAX_QUEUE);  // a busy channel left playing mustn't pile up forever
    if (!this.current) this.#next();
  }

  skip() {
    this.#next();
  }

  hold() {
    this.held = this.held === null && this.current ? this.current.target_id : null;
  }

  #next() {
    if (this.current) this.played = [this.current, ...this.played].slice(0, 10);
    const index = this.held === null ? 0 : this.queue.findIndex((call) => call.target_id === this.held);
    if (!this.on || index < 0 || !this.queue.length) {
      this.current = null;
      return;
    }
    this.current = this.queue[index];
    this.queue = this.queue.filter((_, i) => i !== index);
    this.#audio.src = `api/recordings/${this.current.id}/audio`;
    this.#audio.play().catch(() => {
      // Usually the browser wanting a click first. Turning autoplay on is that click, so this is rare.
      this.failed = 'The browser blocked playback. Press Play once, then it continues by itself.';
    });
  }

  resume() {
    this.failed = '';
    if (this.current) this.#audio?.play().catch(() => {});
    else this.#next();
  }
}
