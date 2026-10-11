/* Service worker — web-push display only. No fetch handler, no
   caching: the SPA stays a plain network app; this worker exists so the
   instance can show a budget-summary notification when the page is
   closed. Payload: {title, body, url} (notify/push.py). */
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { /* text */ }
  const title = data.title || "Oikonome";
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || "",
    icon: "/app/icon.svg",
    badge: "/app/icon.svg",
    data: { url: data.url || "/app/" },
    tag: "oikonome-summary",      // a newer summary replaces the older one
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/app/";
  // An open tab is reused, but a push that names a page (an alert, a
  // report's period) must still land there — focusing alone would leave
  // the person wherever the tab already was. navigate() is refused for a
  // tab this worker does not control; that tab is then just focused.
  event.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true })
    .then((tabs) => {
      for (const t of tabs)
        if (t.url.includes("/app") && "focus" in t)
          return t.focus().then((c) => url === "/app/" || !c.navigate ? c
            : c.navigate(url).catch(() => c));
      return clients.openWindow(url);
    }));
});
