// Apply the pinned theme before first paint (no flash).
// Default is DARK: new instances start dark, matching the dark
// server-rendered auth pages — only an explicit "auto" choice follows the
// OS via the prefers-color-scheme blocks.
// A separate file (not inline) because the SPA CSP is script-src 'self'
// — an inline script is blocked and never ran.
(function () {
  var t = localStorage.getItem("oiko-theme");
  // no stored choice → dark default; "auto" → let the OS blocks decide
  if (t === "light" || t === "dark")
    document.documentElement.dataset.theme = t;
  else if (t !== "auto")
    document.documentElement.dataset.theme = "dark";
})();
