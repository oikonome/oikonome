import { Component, type ReactNode } from "react";
import { readStore, writeStore } from "../safestorage";

// The wording each browser and the bundler's preloader give a page chunk
// that could not be fetched, or that a later deploy removed.
const CHUNK_FAILURE = /dynamically imported module|importing a module script failed|not a valid javascript mime type|unable to preload css/i;

const RELOADED_AT = "oiko-chunk-reload";
const RELOAD_WINDOW_MS = 60_000;

// One automatic reload per minute, so a chunk that stays missing ends on
// the card instead of a loop. A store that does not keep the mark (site data
// blocked) cannot enforce that limit, so it gets no automatic reload at all.
function claimReload(): boolean {
  const last = Number(readStore("session", RELOADED_AT));
  const now = Date.now();
  if (last && now - last < RELOAD_WINDOW_MS) return false;
  writeStore("session", RELOADED_AT, String(now));
  return readStore("session", RELOADED_AT) === String(now);
}

// React unmounts the whole root on a throw nothing catches, which leaves a
// white page with no way out. This catches a page chunk that fails to load
// and any render throw beneath it.
//
// React.lazy keeps a rejected import for the life of the document, so only a
// reload can bring a failed chunk back: a tab left open across a deploy is
// healed that way, and it lands on the page that was asked for. The listener
// is here and not on vite:preloadError because that event also fires for the
// idle warm-up in pageChunks.ts, where a reload would throw away whatever the
// person is typing over a download they never asked for. The server is asked
// first, since reloading with no connection swaps this card for the browser's
// own error page.
export default class ErrorBoundary extends Component<
  { resetKey?: string; children: ReactNode },
  { error: unknown; failed: boolean; resetKey?: string }
> {
  state = { error: null as unknown, failed: false,
            resetKey: this.props.resetKey };

  static getDerivedStateFromError(error: unknown) {
    return { error, failed: true };
  }

  // A new address is a new page: the one that failed must not hold the
  // card over a destination that works.
  static getDerivedStateFromProps(
    props: { resetKey?: string },
    state: { resetKey?: string },
  ) {
    return props.resetKey === state.resetKey ? null
      : { error: null, failed: false, resetKey: props.resetKey };
  }

  componentDidCatch(error: unknown) {
    if (!CHUNK_FAILURE.test(String(error))) return;
    fetch(window.location.href, { cache: "no-store" })
      .then((r) => {
        if (r.ok && this.state.error === error && claimReload())
          window.location.reload();
      })
      .catch(() => { /* no connection: the card's button is the way back */ });
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="centered">
        <div className="card" role="alert">
          <p style={{ marginTop: 0 }}>This page did not load.</p>
          <button type="button" onClick={() => window.location.reload()}>
            Reload</button>
        </div>
      </div>
    );
  }
}
