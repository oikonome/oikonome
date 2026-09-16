// Shared behaviors for the server-rendered (Jinja) pages — external file
// so the pages CSP can drop script-src 'unsafe-inline'. Every
// behavior keys off data attributes / ids; pages without the hooks load
// this as a no-op. Keep it dependency-free and old-browser friendly.
(function () {
  "use strict";

  // PRG notices arrive as ?notice=<code> — show once, then scrub the URL
  // so refresh/bookmark doesn't re-announce a long-finished action
  if (/[?&]notice=/.test(location.search) && history.replaceState) {
    history.replaceState(null, "", location.pathname);
  }

  // [data-localtime]: server renders a UTC ISO timestamp; show it in the
  // VIEWER's own timezone. The element's
  // server text is the pre-JS/UTC fallback, replaced in place here.
  try {
    var LT = { year: "2-digit", month: "2-digit", day: "2-digit",
               hour: "2-digit", minute: "2-digit" };
    var lts = document.querySelectorAll("[data-localtime]");
    for (var li = 0; li < lts.length; li++) {
      var ld = new Date(lts[li].getAttribute("data-localtime"));
      if (!isNaN(ld.getTime())) lts[li].textContent = ld.toLocaleString([], LT);
    }
  } catch (e) { /* leave the UTC fallback */ }

  // form[data-confirm]: native confirm() guard (import.html's undo)
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f && f.getAttribute && f.getAttribute("data-confirm")
        && !window.confirm(f.getAttribute("data-confirm"))) {
      e.preventDefault();
    }
  }, true);

  document.addEventListener("click", function (e) {
    // input[data-select]: select the whole value on click (copy-link box)
    var el = e.target;
    if (el && el.matches && el.matches("input[data-select]")) el.select();
    // button[data-copy="<id>"]: copy that element's value to the clipboard
    var btn = el && el.closest ? el.closest("button[data-copy]") : null;
    if (btn) {
      var src = document.getElementById(btn.getAttribute("data-copy"));
      if (src) {
        src.select();
        if (navigator.clipboard) navigator.clipboard.writeText(src.value);
        btn.textContent = "copied";
      }
    }
  });

  // [data-console-nav]: admin-console section nav. A tablist that
  // shows one .console-view at a time, remembers the choice in localStorage,
  // and deep-links via #hash. (Infra is NOT a view — it's the persistent
  // strip below the nav, refreshed independently in the block further down.)
  var cnav = document.querySelector("[data-console-nav]");
  if (cnav) {
    var VIEW_KEY = "oikonome.admin.view";
    var main = cnav.closest("main") || document.querySelector("main");
    if (main) main.classList.add("nav-ready");   // switch to single-view mode
    var tabs = Array.prototype.slice.call(cnav.querySelectorAll("[data-nav]"));
    var views = document.querySelectorAll(".console-view");
    var names = tabs.map(function (t) { return t.getAttribute("data-nav"); });
    var showView = function (name, moveFocus) {
      if (names.indexOf(name) < 0) name = names[0];
      for (var i = 0; i < tabs.length; i++) {
        var on = tabs[i].getAttribute("data-nav") === name;
        tabs[i].setAttribute("aria-selected", on ? "true" : "false");
        tabs[i].tabIndex = on ? 0 : -1;
        if (on && moveFocus) tabs[i].focus();
      }
      for (var v = 0; v < views.length; v++) {
        views[v].classList.toggle("active",
          views[v].getAttribute("data-view") === name);
      }
      try { localStorage.setItem(VIEW_KEY, name); } catch (e) { /* private */ }
      if (history.replaceState) history.replaceState(null, "", "#" + name);
    };
    cnav.addEventListener("click", function (e) {
      var t = e.target && e.target.closest
        ? e.target.closest("[data-nav]") : null;
      if (t) {
        showView(t.getAttribute("data-nav"), false);
        // Drop focus on MOUSE activation (e.detail>0; a keyboard "click"
        // via Enter reports 0 and keeps focus). The rail opens on
        // :focus-within — the keyboard's hover — so a clicked tab that
        // keeps focus holds the rail open long after the pointer leaves.
        // Same rule as the app rail (App.tsx).
        if (e.detail > 0) t.blur();
      }
    });
    // arrow / home / end keyboard navigation for the tablist
    cnav.addEventListener("keydown", function (e) {
      var i = tabs.indexOf(document.activeElement);
      if (i < 0) return;
      var j = i;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") j = (i + 1) % tabs.length;
      else if (e.key === "ArrowLeft" || e.key === "ArrowUp") j = (i - 1 + tabs.length) % tabs.length;
      else if (e.key === "Home") j = 0;
      else if (e.key === "End") j = tabs.length - 1;
      else return;
      e.preventDefault();
      showView(tabs[j].getAttribute("data-nav"), true);
    });
    // initial view: a #hash deep-link wins, else the last-viewed section,
    // else the first tab
    var initial = (location.hash || "").replace(/^#/, "");
    if (names.indexOf(initial) < 0) {
      try { initial = localStorage.getItem(VIEW_KEY) || names[0]; }
      catch (e) { initial = names[0]; }
    }
    showView(initial, false);

    // left-rail pin (wide screens; CSS does the layout). The app rail's
    // semantics exactly: default is the collapsed
    // hover-open rail, the pin keeps it open, the choice persists. Same
    // words as the app's pin too — two rails with two vocabularies read
    // as two different features.
    var pin = cnav.querySelector("[data-rail-pin]");
    if (pin) {
      var PIN_KEY = "oikonome.admin.railpin";
      var setPin = function (pinned) {
        document.body.classList.toggle("cn-pinned", pinned);
        pin.setAttribute("aria-pressed", pinned ? "true" : "false");
        pin.title = pinned ? "Unpin the sidebar" : "Keep the sidebar open";
        pin.setAttribute("aria-label", pin.title);
        var pt = pin.querySelector(".cn-t");
        if (pt) pt.textContent = pinned ? "Unpin" : "Keep open";
        try { localStorage.setItem(PIN_KEY, pinned ? "1" : "0"); }
        catch (e) { /* private mode */ }
      };
      var p0 = "0";
      try { p0 = localStorage.getItem(PIN_KEY) || "0"; } catch (e) { /* */ }
      setPin(p0 === "1");
      pin.addEventListener("click", function (e) {
        setPin(!document.body.classList.contains("cn-pinned"));
        // Drop focus on mouse activation, same as the tabs above:
        // unpinning leaves focus on this button, and :focus-within then
        // holds the rail open after the pointer has gone.
        if (e.detail > 0) pin.blur();
      });
    }
    // The open rail is as wide as its longest label, not the guessed 200px
    // (the app rail derives its width the same way). Measure each item's
    // natural width and hand it to the CSS var the layout reads.
    var need = 0;
    var items = cnav.querySelectorAll("[data-nav],[data-rail-pin]");
    for (var m = 0; m < items.length; m++) {
      var prevW = items[m].style.width;
      items[m].style.width = "max-content";
      need = Math.max(need, items[m].offsetWidth);
      items[m].style.width = prevW;
    }
    if (need) {
      var cs = getComputedStyle(cnav);
      document.body.style.setProperty("--cnw-open",
        Math.ceil(need + parseFloat(cs.paddingLeft) * 2 + 1) + "px");
    }
  }

  // create-instance free-period select: the day-count input only
  // matters for the "days" choice, so it hides otherwise (CSP: no
  // inline script — behavior lives here, keyed on data attributes).
  // every free-period select on the page (create-instance AND mint-invite
  // carry one), each paired with the day-count input in its OWN form
  var fks = document.querySelectorAll("[data-free-kind]");
  for (var fi = 0; fi < fks.length; fi++) {
    (function (fk) {
      var form = fk.closest("form");
      var fd = form && form.querySelector("[data-free-days]");
      var syncFree = function () {
        if (fd) fd.hidden = fk.value !== "days";
      };
      fk.addEventListener("change", syncFree);
      syncFree();
    })(fks[fi]);
  }

  // [data-collapse="<storage-key>"]: a generic collapsed-by-default card
  // (admin console's Audit trail). Same contract as the
  // infra panel: collapsed unless localStorage holds "0"; a click / Enter /
  // Space on [data-collapse-toggle] flips it and persists the choice. Kept
  // independent of the nav + infra blocks so none can suppress another.
  var collapsers = document.querySelectorAll("[data-collapse]");
  for (var cIdx = 0; cIdx < collapsers.length; cIdx++) {
    (function (panel) {
      var KEY = panel.getAttribute("data-collapse");
      var head = panel.querySelector("[data-collapse-toggle]");
      var apply = function () {
        var v;
        try { v = localStorage.getItem(KEY); } catch (e) { v = null; }
        var collapsed = v !== "0";   // default (nothing stored) = collapsed
        panel.classList.toggle("collapsed", collapsed);
        if (head) head.setAttribute("aria-expanded", collapsed ? "false" : "true");
      };
      apply();
      var toggle = function () {
        var nowCollapsed = !panel.classList.contains("collapsed");
        panel.classList.toggle("collapsed", nowCollapsed);
        if (head) head.setAttribute("aria-expanded", nowCollapsed ? "false" : "true");
        try { localStorage.setItem(KEY, nowCollapsed ? "1" : "0"); }
        catch (e) { /* private mode — state just won't persist */ }
      };
      if (head) {
        head.addEventListener("click", toggle);
        head.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); }
        });
      }
    })(collapsers[cIdx]);
  }

  // #infra-panel[data-infra-refresh]: the admin console's infra-stress
  // strip. A persistent strip below the nav rather than a tab, so it
  // refreshes on every view. To keep that cheap while collapsed, the 20s tick
  // fetches only the one-line SUMMARY (?summary=1 → latest-row only); the
  // heavy chart fragment (full-window scan + SVG) is fetched only while the
  // strip is EXPANDED, and once immediately on expand. Non-OK responses
  // (session expired → 401) stop the loop rather than swapping the panel for
  // an error page; a fetch failure just tries again later.
  var infra = document.getElementById("infra-panel");
  if (infra && infra.hasAttribute("data-infra-refresh")) {
    // Infra strip is ALWAYS EXPANDED — no collapse toggle.
    // `collapsed` is never set, so refreshInfra always pulls the full chart
    // fragment and CSS always shows the detail view (summary stays hidden).
    var refreshInfra;                // assigned once bindInfraCharts exists
    infra.classList.remove("collapsed");
    // One shared tooltip for every chart. Vanilla pointer
    // handling — no chart library. Re-bound after each fragment swap so it
    // survives the 20s auto-refresh (the swap replaces every .infra-chart).
    var tip = document.createElement("div");
    tip.setAttribute("role", "status");
    tip.style.cssText =
      "position:fixed;z-index:50;pointer-events:none;display:none;"
      + "background:var(--card,#1a212b);color:var(--ink,#dbe3ea);"
      + "border:1px solid var(--line,#2a323d);border-radius:6px;"
      + "padding:4px 8px;font-size:12px;line-height:1.4;white-space:nowrap;"
      + "box-shadow:0 4px 14px rgba(0,0,0,.4);transform:translate(-50%,-125%)";
    document.body.appendChild(tip);
    var LTT = { month: "2-digit", day: "2-digit", hour: "2-digit",
                minute: "2-digit" };
    var hideTip = function () { tip.style.display = "none"; };

    var bindInfraCharts = function () {
      var charts = infra.querySelectorAll(".infra-chart[data-chart-points]");
      for (var ci = 0; ci < charts.length; ci++) {
        (function (chart) {
          var svg = chart.querySelector("svg");
          if (!svg) return;
          var pts, vw, vh;
          try { pts = JSON.parse(chart.getAttribute("data-chart-points")); }
          catch (e) { return; }
          if (!pts || !pts.length) return;
          vw = parseFloat(chart.getAttribute("data-chart-vw")) || 480;
          vh = parseFloat(chart.getAttribute("data-chart-vh")) || 150;
          var label = chart.getAttribute("data-chart-label") || "";
          var move = function (ev) {
            var rect = svg.getBoundingClientRect();
            if (!rect.width) return;
            var vbx = (ev.clientX - rect.left) * vw / rect.width;
            var best = pts[0], bd = Math.abs(pts[0].x - vbx);
            for (var i = 1; i < pts.length; i++) {
              var d = Math.abs(pts[i].x - vbx);
              if (d < bd) { bd = d; best = pts[i]; }
            }
            var cx = rect.left + best.x * rect.width / vw;
            var cy = rect.top + best.y * rect.height / vh;
            var when = new Date(best.t);
            var ts = isNaN(when.getTime())
              ? best.t : when.toLocaleString([], LTT);
            tip.innerHTML = "";
            var b = document.createElement("b");
            b.textContent = label;
            var s = document.createElement("span");
            s.style.cssText = "display:block;color:var(--mut,#8b98a3)";
            s.textContent = ts;
            tip.appendChild(document.createTextNode(best.d + " "));
            tip.appendChild(b);
            tip.appendChild(s);
            tip.style.left = cx + "px";
            tip.style.top = cy + "px";
            tip.style.borderColor = best.lv === "red" ? "var(--red,#e0695d)"
              : best.lv === "amber" ? "var(--amber,#f0b429)"
              : "var(--line,#2a323d)";
            tip.style.display = "block";
          };
          svg.addEventListener("pointermove", move);
          svg.addEventListener("pointerdown", move);
          svg.addEventListener("pointerleave", hideTip);
        })(charts[ci]);
      }
    };
    bindInfraCharts();               // the server-rendered first paint

    var infraStopped = false;
    // Collapsed → cheap summary (?summary=1, latest row only); expanded →
    // the full chart fragment. Never runs the expensive full-window query
    // while collapsed. Shared by the 20s tick and the expand handler.
    refreshInfra = function () {
      if (infraStopped) return;
      var win = encodeURIComponent(infra.getAttribute("data-win") || "24h");
      var expanded = !infra.classList.contains("collapsed");
      fetch("/admin/console/infra?win=" + win + (expanded ? "" : "&summary=1"))
        .then(function (r) {
          if (r.status === 401 || r.status === 404) { infraStopped = true; return null; }
          return r.ok ? r.text() : null;
        })
        .then(function (html) {
          if (html) {
            hideTip();
            infra.innerHTML = html;
            bindInfraCharts();       // re-bind: the swap replaced every chart
          }
        })
        .catch(function () { /* transient — next tick retries */ });
    };
    setInterval(function () {
      if (infraStopped || document.hidden) return;
      refreshInfra();
    }, 20000);
  }

  // #linkwait: the Plaid hosted-link waiting page (link.html). Opens the
  // bank tab, then polls the link-session status until done.
  var w = document.getElementById("linkwait");
  if (w) {
    var t0 = Date.now(), done = false;
    try { window.open(w.getAttribute("data-url"), "_blank", "noopener"); }
    catch (e) { /* popup blocked — the page keeps its manual link */ }
    var finish = function (ok, msg) {
      done = true;
      var spin = document.getElementById("spin"),
          strip = document.getElementById("strip");
      spin.style.animation = "none"; spin.style.border = "none";
      spin.textContent = ok ? "✓" : "✗";
      spin.style.color = ok ? "var(--green)" : "var(--red)";
      strip.style.borderColor = ok ? "var(--green)" : "var(--red)";
      document.getElementById("txt").textContent = msg;
      if (ok) setTimeout(function () { location.href = "/accounts"; }, 1800);
    };
    var tick = function () {
      if (done) return;
      document.getElementById("elapsed").textContent =
        Math.round((Date.now() - t0) / 1000) + "s";
      fetch("/accounts/plaid/link/"
            + encodeURIComponent(w.getAttribute("data-token")) + "/status")
        .then(function (r) { return r.json(); })
        .then(function (s) {
          if (s.done && s.kind === "exited") {
            finish(false, "No bank was linked — the sign-in window was "
                          + "closed. Head back to Accounts to try again.");
          } else if (s.done && (s.kind === "update_failed" || s.kind === "add_failed")) {
            finish(false, s.kind === "add_failed"
                          ? ("Link failed"
                             + (s.error ? " — " + s.error : ""))
                          : ("Reconnect didn't complete — "
                             + (s.institution || "the connection")
                             + " still reports an error"
                             + (s.error ? " (" + s.error + ")" : "")));
          } else if (s.done) {
            finish(true, (s.kind === "update" ? "Reconnected " : "Linked ")
                          + (s.institution || "") + " — syncing…");
          } else {
            setTimeout(tick, 3000);
          }
        }).catch(function () { setTimeout(tick, 4000); });
    };
    setTimeout(tick, 2500);
  }

  // ---- Login page: two-step password→TOTP, passkey, demo-cred prefill.
  //      /login is the only login UI. JS posts to /api/login so step 2 is
  //      code-only (email+password stay filled but hidden — never re-typed).
  //      No-JS still posts the HTML form (may re-ask password on step 2).
  var pkBtn = document.getElementById("passkey-btn");
  var loginForm = document.getElementById("login-form")
    || document.querySelector('form[action="/login"]');
  if (loginForm) {
    var emailEl = loginForm.querySelector('#login-step-cred input[name="email"]')
      || loginForm.querySelector('input[name="email"]');
    var pwEl = loginForm.querySelector('#login-step-cred input[name="password"]')
      || loginForm.querySelector('input[name="password"]');
    var totpEl = document.getElementById("login-totp")
      || loginForm.querySelector('input[name="totp_code"]');
    var stepCred = document.getElementById("login-step-cred");
    var step2fa = document.getElementById("login-step-2fa");
    var loginAs = document.getElementById("login-as");
    var loginErr = document.getElementById("login-err");
    var submitBtn = document.getElementById("login-submit")
      || loginForm.querySelector('button[type="submit"], button.pri');
    var pkErr = document.getElementById("passkey-err");
    var savedEmail = "";
    var savedPw = "";
    var mode2fa = null; // "totp" | "passkey_only" | null
    // Sign-in is risk-gated server-side, so a token is usually NOT required.
    // The server says so at render time (data-required); a human_check_failed
    // response flips it on for the rest of this page's life.
    var tsBox = document.getElementById("login-turnstile");
    var tsRequired = !!(tsBox && tsBox.getAttribute("data-required") === "1");
    // A sign-in parked waiting for a Turnstile token, resumed by the
    // widget's data-callback the instant one arrives. Posting on a timer
    // whether or not a token has shown up makes an escalated widget answer
    // "please complete the human check" stacked ABOVE a checkbox the user
    // has not noticed yet — the error and its own remedy, in the wrong
    // order.
    var tsPending = null;
    window.oikoTurnstileToken = function () {
      var resume = tsPending;
      tsPending = null;
      if (resume) resume();
    };

    var showErr = function (m) {
      if (loginErr) {
        loginErr.textContent = m || "";
        loginErr.style.display = m ? "" : "none";
      }
      if (pkErr) {
        if (m && mode2fa === "passkey_only") {
          pkErr.textContent = m; pkErr.style.display = "";
        } else { pkErr.style.display = "none"; }
      }
    };
    var enter2fa = function (kind, detail) {
      mode2fa = kind;
      var hint = document.getElementById("login-2fa-hint");
      var lab = document.getElementById("login-2fa-label");
      // Refresh totpEl — step-2 DOM may have been server-rendered only
      totpEl = document.getElementById("login-totp")
        || loginForm.querySelector('input[name="totp_code"]');
      if (kind === "passkey_only") {
        if (hint) hint.innerHTML = "This account signs in with a passkey. Use "
          + "the button below, or paste a one-time <b>recovery code</b> if "
          + "you lost the device.";
        if (lab) {
          // label text node before the <br>/input
          var first = lab.firstChild;
          if (first && first.nodeType === 3) first.textContent = "Recovery code";
        }
        if (totpEl) {
          totpEl.placeholder = "xxxx-xxxx-xxxx-xxxx-xxxx";
          totpEl.setAttribute("autocomplete", "off");
        }
      } else {
        if (hint) hint.innerHTML = "6-digit authenticator code — or a one-time "
          + "<b>recovery code</b> if you lost your app. Prefer a passkey? "
          + "Use the button below when enrolled.";
        if (lab) {
          var first2 = lab.firstChild;
          if (first2 && first2.nodeType === 3)
            first2.textContent = "Authenticator or recovery code";
        }
        if (totpEl) {
          totpEl.placeholder = "6-digit code or recovery code";
          totpEl.setAttribute("autocomplete", "one-time-code");
        }
      }
      // Hide step 1 but keep values; drop required so hidden fields don't
      // block HTML5 validation on the code-only submit.
      if (stepCred) {
        stepCred.style.display = "none";
        var reqs = stepCred.querySelectorAll("[required]");
        for (var ri = 0; ri < reqs.length; ri++) reqs[ri].required = false;
      }
      if (step2fa) step2fa.style.display = "";
      if (loginAs) {
        loginAs.innerHTML = "Signing in as <b></b>";
        var b = loginAs.querySelector("b");
        if (b) b.textContent = savedEmail || (emailEl && emailEl.value) || "";
      }
      if (submitBtn) submitBtn.textContent = "Sign in with code";
      loginForm.setAttribute("data-step", "2fa");
      if (totpEl) { totpEl.required = true; try { totpEl.focus(); } catch (ignore) {} }
      if (detail) showErr(detail);
    };

    var b64uToBuf = function (s) {
      s = String(s).replace(/-/g, "+").replace(/_/g, "/");
      while (s.length % 4) s += "=";
      var bin = atob(s), b = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) b[i] = bin.charCodeAt(i);
      return b.buffer;
    };
    var bufToB64u = function (buf) {
      var b = new Uint8Array(buf), s = "";
      for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
      return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    };

    // API-driven two-step: password once, then code only
    loginForm.addEventListener("submit", function (e) {
      e.preventDefault();
      showErr("");
      // Drop any sign-in parked by an earlier click: this submit supersedes
      // it, and leaving it armed would let a late data-callback post twice.
      tsPending = null;
      if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = "…"; }
      if (!mode2fa) {
        savedEmail = emailEl ? emailEl.value.trim() : "";
        savedPw = pwEl ? pwEl.value : "";
      }
      // Prefer values held from step 1; fall back to any form field (no-JS
      // server re-render puts email hidden + password on step 2).
      var email = savedEmail
        || (emailEl && emailEl.value)
        || ((loginForm.querySelector('input[name="email"]') || {}).value)
        || "";
      var password = savedPw
        || (pwEl && pwEl.value)
        || ((loginForm.querySelector('input[name="password"]') || {}).value)
        || "";
      totpEl = document.getElementById("login-totp")
        || loginForm.querySelector('input[name="totp_code"]');
      var totp = totpEl ? totpEl.value : "";
      var body = new URLSearchParams();
      body.set("email", email);
      body.set("password", password);
      if (totp) body.set("totp_code", totp);
      // Turnstile: the widget writes a hidden field into the form. Whatever
      // token is sitting there always rides along — it costs nothing and
      // clears the check if the server happens to want one. We only WAIT for
      // a token when the server has said it will enforce it (tsRequired):
      // waiting otherwise would make every sign-in hang behind a script
      // an ad blocker may never let load.
      function token() {
        var el = loginForm.querySelector('[name="cf-turnstile-response"]');
        return el ? el.value : "";
      }
      function send() {
        if (token()) body.set("cf-turnstile-response", token());
        post();
      }
      if (tsBox && tsRequired && !token()) {
        // The invisible challenge is normally sub-second, so give it a
        // short grace period before concluding anything.
        var waited = 0;
        (function wait() {
          if (token()) { send(); return; }
          if (waited >= 2500) {
            if (window.turnstile) {
              // The runtime is up and still has no token for us, which means
              // Cloudflare escalated and is showing something to click. Park
              // the sign-in — data-callback fires it the moment the user
              // solves it — instead of posting a request we know will fail.
              tsPending = send;
              if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.textContent = mode2fa ? "Sign in with code" : "Sign in";
              }
              showErr("Complete the human check above — sign-in continues "
                      + "by itself once you do.");
            } else {
              // No Turnstile runtime at all (blocked script, DNS, offline):
              // no callback is ever coming, so post and let the server answer
              // rather than leave the page waiting forever.
              send();
            }
            return;
          }
          waited += 200;
          setTimeout(wait, 200);
        })();
      } else {
        send();
      }
      return;

      function post() {
      fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
        credentials: "same-origin"
      }).then(function (r) {
        return r.text().then(function (t) {
          var detail = t;
          try {
            var j = JSON.parse(t);
            detail = j.detail || j.message || t;
          } catch (ignore) { /* plain text */ }
          return { ok: r.ok, status: r.status, detail: String(detail) };
        });
      }).then(function (res) {
        if (res.ok) {
          window.location.href = "/app/";
          return;
        }
        // A Turnstile token is SINGLE-USE, and two-step sign-in posts twice
        // (password, then code). Reset after every non-success so step 2 —
        // and any retry — has a fresh one waiting.
        if (window.turnstile) { try { window.turnstile.reset(); } catch (ignore) { /* widget gone */ } }
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = mode2fa ? "Sign in with code" : "Sign in";
        }
        var d = res.detail || "";
        if (d.indexOf("human_check_failed") >= 0) {
          // Risk gate just closed on this IP or account. Every later attempt
          // needs a real token, so wait for one from here on.
          tsRequired = true;
          // The server-rendered page offers "Email me a sign-in
          // link" when a check is demanded, but this branch never re-renders
          // the DOM — so a challenge that escalates MID-SESSION (after the
          // initial GET computed show_unlock) leaves the user staring at a
          // widget they may be unable to complete, with the escape hatch
          // reachable only by a full reload nothing here triggers. Name the
          // route in the error instead: /unlock works whether or not the
          // widget ever loads, which is the whole point of it.
          var blocked = tsBox && !window.turnstile;
          showErr(blocked
            ? "The human check could not load — it may be blocked by an "
              + "extension or your network. Go to /unlock and we will email "
              + "you a sign-in link instead."
            : (tsBox
              ? "Please complete the human check below, then sign in again. "
                + "Cannot see it? Go to /unlock for an emailed sign-in link."
              : "Please complete the human check and try again, or go to "
                + "/unlock for an emailed sign-in link."));
          return;
        }
        if (d.indexOf("totp_required") >= 0) {
          enter2fa("totp", "");
          return;
        }
        if (d.indexOf("passkey_required") >= 0) {
          enter2fa("passkey_only",
            "Use your passkey below, or enter a recovery code.");
          // Prefer passkey when available; recovery field stays as escape.
          if (pkBtn && pkBtn.style.display !== "none") {
            try { pkBtn.click(); } catch (ignore) { /* user can click */ }
          }
          return;
        }
        if (res.status === 429) {
          showErr("Too many failed attempts — try again in a few minutes.");
          return;
        }
        // A 5xx is OUR fault, not theirs. Falling through to the generic
        // branch would tell a user with correct credentials that their
        // password was wrong during an outage, and send them off to reset a
        // password that is fine.
        if (res.status >= 500) {
          showErr("Couldn't reach the server — try again.");
          return;
        }
        showErr(mode2fa
          ? "That code didn't match — check your authenticator or recovery code."
          : "Wrong email or password.");
      }).catch(function () {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = mode2fa ? "Sign in with code" : "Sign in";
        }
        showErr("Couldn't reach the server — try again.");
      });
      }   // post()
    });

    // If the server already rendered step 2 (no prior JS), upgrade to
    // code-only once the user has typed password into the fallback field —
    // or, if step1 inputs exist with values (bfcache), hide cred.
    if (loginForm.getAttribute("data-step") === "2fa") {
      mode2fa = document.getElementById("login-2fa-hint")
        && /passkey/i.test((document.getElementById("login-2fa-hint") || {}).textContent || "")
        ? "passkey_only" : "totp";
      savedEmail = emailEl ? emailEl.value : "";
    }

    if (pkBtn) {
      // passkeys need https + WebAuthn — show the button only where it works
      if (window.isSecureContext && window.PublicKeyCredential)
        pkBtn.style.display = "";

      pkBtn.addEventListener("click", function () {
        if (pkErr) pkErr.style.display = "none";
        showErr("");
        // Email is optional: empty → discoverable-credential flow (browser
        // picks the passkey). If the user already typed an email we pass it
        // so non-resident keys still get an allow-list.
        var em = (savedEmail || (emailEl && emailEl.value) || "").trim();
        pkBtn.disabled = true;
        fetch("/api/login/passkey/options", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(em ? { email: em } : {})
        }).then(function (r) {
          if (!r.ok) throw new Error("options"); return r.json();
        }).then(function (o) {
          var pk = (o.options && o.options.publicKey) || o.options;
          pk.challenge = b64uToBuf(pk.challenge);
          if (pk.allowCredentials && pk.allowCredentials.length) {
            pk.allowCredentials = pk.allowCredentials.map(function (c) {
              return { id: b64uToBuf(c.id), type: c.type, transports: c.transports };
            });
          } else {
            // discoverable / usernameless — empty allow-list confuses some
            // browsers; omit the field entirely
            delete pk.allowCredentials;
          }
          return navigator.credentials.get({ publicKey: pk }).then(function (cred) {
            var rr = cred.response;
            // Encode rawId once and use it for BOTH id and rawId so the
            // server's py_webauthn check (b64u(raw_id) == id) always holds.
            var rawB64 = bufToB64u(cred.rawId);
            var credential = {
              id: rawB64, type: cred.type || "public-key", rawId: rawB64,
              clientExtensionResults: cred.getClientExtensionResults
                ? cred.getClientExtensionResults() : {},
              response: {
                authenticatorData: bufToB64u(rr.authenticatorData),
                clientDataJSON: bufToB64u(rr.clientDataJSON),
                signature: bufToB64u(rr.signature)
              }
            };
            if (rr.userHandle)
              credential.response.userHandle = bufToB64u(rr.userHandle);
            return fetch("/api/login/passkey", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ challenge_id: o.challenge_id, credential: credential })
            }).then(function (r) {
              return r.text().then(function (t) {
                var detail = t;
                try {
                  var j = JSON.parse(t);
                  detail = j.detail || j.message || t;
                } catch (ignore) { /* plain */ }
                if (!r.ok) {
                  var err = new Error(String(detail || "verify"));
                  err.name = "VerifyError";
                  throw err;
                }
                return r;
              });
            });
          });
        }).then(function () {
          window.location.href = "/app/";
        }).catch(function (e) {
          pkBtn.disabled = false;
          if (e && e.name === "NotAllowedError") return;  // user cancelled
          var detail = (e && e.message) ? String(e.message) : "";
          if (detail && detail !== "options" && detail !== "verify"
              && detail.length < 180 && detail.indexOf("<") < 0)
            showErr("Passkey sign-in failed: " + detail
              + " — try password + authenticator, or re-add the passkey in Settings.");
          else
            showErr("Passkey sign-in didn't work — use password + authenticator "
              + "(or a recovery code), or re-add the passkey in Settings after signing in.");
        });
      });
    }

    // demo instance: prefill the printed synthetic credentials
    fetch("/api/demo-login").then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (d) {
      if (d && d.demo && d.email && d.password) {
        if (emailEl && !emailEl.value) emailEl.value = d.email;
        if (pwEl && !pwEl.value) pwEl.value = d.password;
        var note = document.getElementById("demo-note");
        if (note) {
          note.textContent = "Demo instance — sample credentials are filled in; just press Sign in.";
          note.style.display = "";
        }
      }
    }).catch(function () {});
  }

  // ---- admin-console operator passkeys ------------------------
  // Three behaviors on one page: sign in with a key, enrol a key, and get a
  // FRESH assertion before a destructive host command (or before removing a
  // key). The step-up interception fails CLOSED — the server refuses the
  // command without a live ticket, so a JS-less browser cannot slip past it,
  // it just cannot run those commands.
  (function () {
    var loginBtn = document.querySelector("[data-admin-passkey-login]");
    var enrolForm = document.querySelector("[data-admin-passkey-enrol]");
    var stepupForms = document.querySelectorAll("form[data-admin-stepup]");
    if (!loginBtn && !enrolForm && !stepupForms.length) return;

    // local copies — the login-page helpers above are scoped to that block
    var toBuf = function (s) {
      s = String(s).replace(/-/g, "+").replace(/_/g, "/");
      while (s.length % 4) s += "=";
      var bin = atob(s), b = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) b[i] = bin.charCodeAt(i);
      return b.buffer;
    };
    var toB64u = function (buf) {
      var b = new Uint8Array(buf), s = "";
      for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
      return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    };
    var say = function (near, msg) {
      var box = null, n = near;
      while (n && !box) { box = n.querySelector ? n.querySelector("[data-admin-passkey-error]") : null; n = n.parentElement; }
      if (!box) box = document.querySelector("[data-admin-passkey-error]");
      if (!box) { if (msg) window.alert(msg); return; }
      box.textContent = msg || "";
      if (msg) box.removeAttribute("hidden"); else box.setAttribute("hidden", "");
    };
    var post = function (url, body) {
      return fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {})
      }).then(function (r) {
        return r.text().then(function (t) {
          var j = null;
          try { j = JSON.parse(t); } catch (ignore) { /* html/plain */ }
          if (!r.ok) throw new Error((j && (j.error || j.detail)) || "request failed");
          return j || {};
        });
      });
    };
    var friendly = function (e) {
      if (e && e.name === "NotAllowedError") return "";      // user cancelled
      var m = (e && e.message) ? String(e.message) : "";
      return (m && m.length < 180 && m.indexOf("<") < 0) ? m
        : "That didn't work — try again, or use the operator token.";
    };
    var assert = function (optsUrl, verifyUrl) {
      return post(optsUrl, {}).then(function (o) {
        var pk = (o.options && o.options.publicKey) || o.options;
        pk.challenge = toBuf(pk.challenge);
        if (pk.allowCredentials && pk.allowCredentials.length) {
          pk.allowCredentials = pk.allowCredentials.map(function (c) {
            return { id: toBuf(c.id), type: c.type, transports: c.transports };
          });
        } else { delete pk.allowCredentials; }
        return navigator.credentials.get({ publicKey: pk }).then(function (cred) {
          var rr = cred.response, raw = toB64u(cred.rawId);
          var credential = {
            id: raw, rawId: raw, type: cred.type || "public-key",
            clientExtensionResults: cred.getClientExtensionResults
              ? cred.getClientExtensionResults() : {},
            response: {
              authenticatorData: toB64u(rr.authenticatorData),
              clientDataJSON: toB64u(rr.clientDataJSON),
              signature: toB64u(rr.signature)
            }
          };
          if (rr.userHandle) credential.response.userHandle = toB64u(rr.userHandle);
          return post(verifyUrl, { challenge_id: o.challenge_id, credential: credential });
        });
      });
    };

    if (loginBtn) {
      loginBtn.addEventListener("click", function () {
        say(loginBtn, "");
        loginBtn.disabled = true;
        assert("/admin/console/passkey/login/options", "/admin/console/passkey/login")
          .then(function (r) { location.assign((r && r.next) || "/admin/console"); })
          .catch(function (e) { loginBtn.disabled = false; say(loginBtn, friendly(e)); });
      });
    }

    if (enrolForm) {
      enrolForm.addEventListener("submit", function (e) {
        e.preventDefault();
        say(enrolForm, "");
        var label = (enrolForm.querySelector("[name=label]") || {}).value || "";
        var btn = enrolForm.querySelector("button");
        // data-ticket: the host-issued recovery link stands in for a session
        // (passkey-only console — there is no token to sign in with first)
        var ticket = enrolForm.getAttribute("data-ticket") || "";
        if (btn) btn.disabled = true;
        // data-stepup: a key already exists, so the server demands a fresh
        // assertion with one you HOLD before it will store another.
        // Collect that first — otherwise the user completes a whole
        // WebAuthn ceremony and gets a 403 at the end of it.
        var pre = enrolForm.getAttribute("data-stepup")
          ? assert("/admin/console/passkey/stepup/options",
                   "/admin/console/passkey/stepup")
              .then(function (r) { return (r && r.stepup_ticket) || ""; })
          : Promise.resolve("");
        pre.then(function (stepup) {
        return post("/admin/console/passkey/options", { label: label, ticket: ticket }).then(function (o) {
          var pk = (o.options && o.options.publicKey) || o.options;
          pk.challenge = toBuf(pk.challenge);
          pk.user.id = toBuf(pk.user.id);
          if (pk.excludeCredentials && pk.excludeCredentials.length) {
            pk.excludeCredentials = pk.excludeCredentials.map(function (c) {
              return { id: toBuf(c.id), type: c.type, transports: c.transports };
            });
          } else { delete pk.excludeCredentials; }
          return navigator.credentials.create({ publicKey: pk }).then(function (cred) {
            var rr = cred.response, raw = toB64u(cred.rawId);
            var credential = {
              id: raw, rawId: raw, type: cred.type || "public-key",
              clientExtensionResults: cred.getClientExtensionResults
                ? cred.getClientExtensionResults() : {},
              response: {
                attestationObject: toB64u(rr.attestationObject),
                clientDataJSON: toB64u(rr.clientDataJSON),
                transports: rr.getTransports ? rr.getTransports() : []
              }
            };
            return post("/admin/console/passkey", {
              challenge_id: o.challenge_id, credential: credential,
              label: label, ticket: ticket, stepup: stepup
            });
          });
        });
        }).then(function () { location.assign("/admin/console#ops"); })
          .catch(function (err) {
            if (btn) btn.disabled = false;
            say(enrolForm, friendly(err));
          });
      });
    }

    for (var i = 0; i < stepupForms.length; i++) {
      (function (form) {
        form.addEventListener("submit", function (e) {
          var field = form.querySelector("[name=stepup]");
          if (!field || field.value) return;          // already ticketed — let it go
          e.preventDefault();
          say(form, "");
          var btn = form.querySelector("button");
          if (btn) btn.disabled = true;
          assert("/admin/console/passkey/stepup/options", "/admin/console/passkey/stepup")
            .then(function (r) {
              field.value = (r && r.stepup_ticket) || "";
              if (btn) btn.disabled = false;
              if (!field.value) throw new Error("no confirmation issued");
              form.submit();
            })
            .catch(function (err) {
              if (btn) btn.disabled = false;
              say(form, friendly(err));
            });
        });
      }(stepupForms[i]));
    }
  }());

  // "did you mean gmail.com?" under the signup email field.
  //
  // A typo'd domain is accepted by every validity check there is — it parses,
  // it has an @ and a dot, and it is often a registered domain — so the first
  // and only sign that anything is wrong is mail that never arrives.
  // Catching it HERE is worth more than catching it anywhere else, because
  // this is the one moment the person is still looking at what they typed.
  //
  // A suggestion, never a block: the form submits unchanged whatever this
  // says. Clicking the suggestion accepts it; ignoring it costs nothing. Some
  // real domains do look like typos of big ones, and arguing with someone
  // about their own email address is a worse failure than a rare bounce.
  //
  // The same table lives server-side in notify/emailhint.py (which is what
  // the settings email-change door uses). Two copies of ~25 domains is the
  // cheaper trade against an unauthenticated lookup endpoint on the signup
  // path — one that would also be a free "does this domain interest you"
  // oracle for anyone probing the form.
  (function () {
    var field = document.querySelector('form[action="/signup"] '
                                       + 'input[type="email"]');
    if (!field) return;
    var COMMON = ["gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
      "live.com", "msn.com", "yahoo.com", "ymail.com", "aol.com",
      "icloud.com", "me.com", "proton.me", "protonmail.com", "pm.me",
      "fastmail.com", "gmx.com", "zoho.com", "yandex.com", "mail.com",
      "hey.com", "comcast.net", "verizon.net", "att.net", "sbcglobal.net",
      "cox.net", "charter.net"];
    var TLD = { "gmail.co": "gmail.com", "gmail.cm": "gmail.com",
      "gmail.con": "gmail.com", "yahoo.co": "yahoo.com",
      "hotmail.co": "hotmail.com", "outlook.co": "outlook.com",
      "icloud.co": "icloud.com" };

    // Damerau-Levenshtein (optimal string alignment) — a transposition is
    // ONE edit. "gmial.com" is the commonest way to misspell "gmail.com",
    // and under plain Levenshtein it scores the same as two unrelated
    // substitutions. Mirror of notify/emailhint.py::_distance.
    function distance(a, b, cap) {
      if (Math.abs(a.length - b.length) > cap) return cap + 1;
      var prev2 = [], prev = [], i, j;
      for (j = 0; j <= b.length; j++) prev[j] = j;
      for (i = 1; i <= a.length; i++) {
        var cur = [i], lo = i;
        for (j = 1; j <= b.length; j++) {
          var d = Math.min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (a.charAt(i - 1) === b.charAt(j - 1)
                                          ? 0 : 1));
          if (i > 1 && j > 1 && a.charAt(i - 1) === b.charAt(j - 2)
              && a.charAt(i - 2) === b.charAt(j - 1)) {
            d = Math.min(d, prev2[j - 2] + 1);
          }
          cur[j] = d;
          if (d < lo) lo = d;
        }
        if (lo > cap) return cap + 1;
        prev2 = prev; prev = cur;
      }
      return prev[b.length];
    }

    function suggest(value) {
      var at = value.indexOf("@");
      if (at < 1 || value.indexOf("@", at + 1) !== -1) return null;
      var local = value.slice(0, at).toLowerCase();
      var domain = value.slice(at + 1).toLowerCase();
      if (!domain || COMMON.indexOf(domain) !== -1) return null;
      if (TLD[domain]) return local + "@" + TLD[domain];
      var best = null, bestD = 3;
      for (var i = 0; i < COMMON.length; i++) {
        var d = distance(domain, COMMON[i], 2);
        var limit = COMMON[i].length <= 9 ? 1 : 2;
        if (d <= limit && d < bestD) { best = COMMON[i]; bestD = d; }
      }
      return best ? local + "@" + best : null;
    }

    var hint = document.createElement("p");
    hint.className = "sub";
    hint.style.margin = "-.4rem 0 .6rem";
    hint.hidden = true;
    (field.closest("p") || field.parentNode).insertAdjacentElement(
      "afterend", hint);

    function check() {
      var s = suggest(field.value.trim());
      if (!s) { hint.hidden = true; hint.textContent = ""; return; }
      hint.textContent = "Did you mean ";
      var a = document.createElement("a");
      a.href = "#";
      a.textContent = s;
      a.addEventListener("click", function (e) {
        e.preventDefault();
        field.value = s;
        hint.hidden = true;
      });
      hint.appendChild(a);
      hint.appendChild(document.createTextNode("?"));
      hint.hidden = false;
    }
    // on blur, not on every keystroke — mid-typing every address looks
    // like a typo of something, and a hint that flickers while you type
    // teaches people to ignore it
    field.addEventListener("blur", check);
  }());
})();
