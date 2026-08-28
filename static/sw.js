/* The Yard — service worker. Handles Web Push while the app is closed. */
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
  event.waitUntil(self.registration.showNotification(title, opts));
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
