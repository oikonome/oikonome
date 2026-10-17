// Apply the pinned theme before first paint (no flash).
// Default is AUTO: with no stored choice the OS preference decides through
// the prefers-color-scheme blocks; only an explicit light or dark pins.
// A separate file (not inline) because the SPA CSP is script-src 'self'
// — an inline script would be blocked.
(function () {
  // storage can be blocked (it then throws); that reads as no choice
  var t = null;
  try { t = localStorage.getItem("oiko-theme"); } catch (e) { /* blocked */ }
  // no stored choice, or "auto" → nothing set: the OS blocks decide
  if (t === "light" || t === "dark")
    document.documentElement.dataset.theme = t;
})();
