// Ask WidgetKit to redraw every widget of this app — called from JS after
// the Today query settles, so opening the app never leaves the
// home-screen widget showing an older verdict than the screen — and drop
// the payload the extension caches for offline redraws.
import ExpoModulesCore
import WidgetKit

public class WidgetReloadModule: Module {
  public func definition() -> ModuleDefinition {
    Name("WidgetReload")

    Function("reload") {
      if #available(iOS 14.0, *) {
        WidgetCenter.shared.reloadAllTimelines()
      }
    }

    // The extension (targets/glance, `Cache`) keeps its last payload in the
    // app group's defaults under these keys. That payload is one
    // household's numbers, and the extension falls back to it on any failed
    // fetch — so at sign-out, and before a different household's credential
    // is written, it has to go, or the widget can show the previous
    // household's day as this one's. The group is the one both targets are
    // granted: `group.<this app's bundle id>`.
    Function("clearCache") {
      guard let id = Bundle.main.bundleIdentifier,
            let d = UserDefaults(suiteName: "group.\(id)") else { return }
      for k in ["glance", "glance_at", "glance_owner"] { d.removeObject(forKey: k) }
    }
  }
}
