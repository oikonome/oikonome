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

    // How many of this app's widgets are placed, so the app can skip the
    // glance refresh when there are none. -1 when WidgetKit cannot say.
    AsyncFunction("placedCount") { (promise: Promise) in
      if #available(iOS 14.0, *) {
        WidgetCenter.shared.getCurrentConfigurations { result in
          switch result {
          case .success(let infos): promise.resolve(infos.count)
          case .failure: promise.resolve(-1)
          }
        }
      } else {
        promise.resolve(0)
      }
    }

    // These app-group defaults keys are a LEGACY location: earlier builds of
    // the extension (targets/glance) kept their last payload here. The live
    // copy is now the Keychain item `oikonome.widgetGlanceExt`, which the
    // JS side (src/lib/glance.ts) deletes itself. A payload left here is
    // still one household's numbers, so at sign-out, and before a
    // different household's credential is written, the old keys are
    // cleared too, or a build that still reads them could show the previous
    // household's day as this one's. The group is the one both targets are
    // granted: `group.<this app's bundle id>`.
    Function("clearCache") {
      guard let id = Bundle.main.bundleIdentifier,
            let d = UserDefaults(suiteName: "group.\(id)") else { return }
      for k in ["glance", "glance_at", "glance_owner"] { d.removeObject(forKey: k) }
    }
  }
}
