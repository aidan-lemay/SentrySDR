<script>
  import { onMount } from 'svelte';
  import Warnings from './Warnings.svelte';
  import { api, setSignedOutHandler } from './lib.js';
  import Auth from './views/Auth.svelte';
  import Sources from './views/Sources.svelte';
  import Schedule from './views/Schedule.svelte';
  import Recordings from './views/Recordings.svelte';
  import Targets from './views/Targets.svelte';
  import Maps from './views/Maps.svelte';
  import Messages from './views/Messages.svelte';
  import Scanner from './views/Scanner.svelte';
  import Alerts from './views/Alerts.svelte';
  import Account from './views/Account.svelte';
  import Admin from './views/Admin.svelte';

  let user = $state(null);
  let loading = $state(true);
  let menuOpen = $state(false);
  let route = $state(location.hash.slice(2) || 'sources');

  const requested = $derived(route.split('?')[0]);
  const can = (permission) => user?.permissions.includes(permission);
  const links = $derived([
    { id: 'sources', label: 'Sources' },
    { id: 'schedule', label: 'Schedule' },
    { id: 'recordings', label: 'Recordings', show: can('listen_recordings') },
    { id: 'scanner', label: 'Scanner', show: can('listen_recordings') },
    { id: 'targets', label: 'Targets', show: can('manage_targets') },
    { id: 'maps', label: 'Maps', show: can('maps') },
    { id: 'messages', label: 'Messages', show: can('maps') },
    { id: 'alerts', label: 'Alerts', show: can('listen_recordings') || can('maps'), count: alertCount },
    { id: 'admin', label: 'Admin', show: user?.role === 'admin' },
    { id: 'account', label: 'My account' },
  ].filter((link) => link.show !== false));
  // Only pages in this user's menu open, even if typed into the address bar. Anything else shows Sources.
  const view = $derived(links.some((link) => link.id === requested) ? requested : 'sources');

  // New keyword matches since this user last looked, for the menu.
  let alertCount = $state(0);
  async function refreshAlerts() {
    if (!user || user.must_change_password || document.hidden) return;
    try {
      alertCount = (await api('alerts')).count;
    } catch {
      // The next check will try again.
    }
  }

  async function refreshUser() {
    try {
      user = await api('me');
    } catch {
      user = null;
    }
  }

  async function signOut() {
    await api('auth/logout', { method: 'POST' }).catch(() => {});
    user = null;
    menuOpen = false;
  }

  onMount(() => {
    setSignedOutHandler(() => { user = null; });
    const onHash = () => {
      route = location.hash.slice(2) || 'sources';
      menuOpen = false;
      scrollTo(0, 0);
    };
    addEventListener('hashchange', onHash);
    refreshUser().finally(() => { loading = false; refreshAlerts(); });
    const alertTimer = setInterval(refreshAlerts, 30000);
    return () => {
      removeEventListener('hashchange', onHash);
      clearInterval(alertTimer);
    };
  });
</script>

{#if loading}
  <p class="pad">Loading...</p>
{:else if !user}
  <Auth onSignedIn={refreshUser} />
{:else}
  <header class="site">
    <div class="site-top">
      <span class="site-name">SentrySDR</span>
      {#if !user.must_change_password}
        <button class="menu-button" aria-expanded={menuOpen} onclick={() => (menuOpen = !menuOpen)}>{menuOpen ? 'Close' : 'Menu'}{#if alertCount && !menuOpen}<span class="nav-count" aria-label={`${alertCount} new alerts`}>{alertCount > 99 ? '99+' : alertCount}</span>{/if}</button>
      {:else}
        <button onclick={signOut}>Sign out</button>
      {/if}
    </div>
    {#if !user.must_change_password}
      <nav class:open={menuOpen} aria-label="Main">
        {#each links as link (link.id)}
          <a href={`#/${link.id}`} aria-current={view === link.id ? 'page' : undefined}>{link.label}{#if link.count}<span class="nav-count" aria-label={`${link.count} new`}>{link.count > 99 ? '99+' : link.count}</span>{/if}</a>
        {/each}
        <button class="nav-out" onclick={signOut}>Sign out ({user.name})</button>
      </nav>
      <div class="who small">
        <span>{user.name}</span>
        <button onclick={signOut}>Sign out</button>
      </div>
    {/if}
  </header>
  <main>
    {#if !user.must_change_password && (user.role === 'admin' || user.role === 'operator')}<Warnings admin={user.role === 'admin'} />{/if}
    {#if user.must_change_password}
      <Account {user} onChange={refreshUser} forced />
    {:else if view === 'schedule'}
      <Schedule {user} />
    {:else if view === 'recordings'}
      <Recordings {user} {route} />
    {:else if view === 'targets'}
      <Targets {user} />
    {:else if view === 'maps'}
      <Maps {user} />
    {:else if view === 'alerts'}
      <Alerts onSeen={refreshAlerts} />
    {:else if view === 'scanner'}
      <Scanner />
    {:else if view === 'messages'}
      <Messages {route} {user} />
    {:else if view === 'account'}
      <Account {user} onChange={refreshUser} />
    {:else if view === 'admin'}
      <Admin />
    {:else}
      <Sources {user} />
    {/if}
  </main>
{/if}
