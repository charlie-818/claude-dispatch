/* The Yard — service worker. Handles Web Push while the app is closed. */
const SW_VERSION = 'report-1';   // bump to see a new worker reach the phone
self.addEventListener('message', e => {
  if (e.data === 'sw-version' && e.source) e.source.postMessage({type: 'sw-version', v: SW_VERSION});
});
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (_) {}
  const title = d.title || 'The Yard';
  const opts = {
    body: d.body || '',
    tag: d.tag || 'yard',
    renotify: true,
    icon: '/icons/icon-192.png',
    badge: '/icons/icon-192.png',
    data: { uuid: d.uuid || '' }
  };
  // Report every push back to the server log: did it reach the phone, what
  // does the browser say about permission, and did showing it throw? A phone
  // has no console, and "accepted by FCM but never seen" is otherwise a guess.
  const report = msg => fetch('/api/clientlog', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({msg: '[sw] ' + msg})}).catch(() => {});
  const perm = (self.Notification && Notification.permission) || '?';
  event.waitUntil(self.registration.showNotification(title, opts)
    .then(() => report(`push shown: ${title} (permission ${perm})`))
    .catch(e => report(`push NOT shown: ${title} (permission ${perm}): ${e && e.message || e}`)));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const uuid = event.notification.data && event.notification.data.uuid;
  const url = uuid ? '/?pane=' + encodeURIComponent(uuid) : '/';
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    // focus an existing tab if we have one, else open a new one
    for (const c of all) {
      if ('focus' in c) {
        c.focus();
        if (uuid && 'postMessage' in c) c.postMessage({ type: 'open-pane', uuid });
        return;
      }
    }
    if (self.clients.openWindow) return self.clients.openWindow(url);
  })());
});
