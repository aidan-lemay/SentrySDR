import { mount } from 'svelte';
import 'leaflet/dist/leaflet.css';
import './app.css';
import App from './App.svelte';

mount(App, { target: document.getElementById('app') });

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  navigator.serviceWorker.register('./sw.js').catch(() => {});
}
