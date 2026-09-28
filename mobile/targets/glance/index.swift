// The iOS home-screen widget: the Today hero's simple face, and nothing
// past it. The extension fetches for itself — the app's device token is
// behind the biometric gate, so the widget polls with its own lesser
// token, read from the Keychain in the app group the app shares with it
// (src/lib/glance.ts writes it there through expo-secure-store). Every
// string is composed by the server; this file mirrors the decisions in
// src/lib/glance-pure.ts — stale after an hour, "Sign in" without a
// credential, "Set a plan" before a budget exists — line for line.
import CryptoKit
import SwiftUI
import WidgetKit

// MARK: - the payload (GET /api/today/glance)

struct Chip: Codable { let text: String; let tone: String }

struct Glance: Codable {
  let date: String
  let budgets_set: Bool
  let verdict: String?
  let left_today: Double?
  let day_spent: Double?
  let day_allow: Double?
  let days_left: Int?
  let pace_line: String?
  let chips: [Chip]?
  let as_of: String
}

// MARK: - the credential, as expo-secure-store stores a plain item

enum Shared {
  /// The app's bundle id is this extension's minus its own suffix.
  static var appBundleId: String {
    let mine = Bundle.main.bundleIdentifier ?? ""
    return mine.hasSuffix(".glance") ? String(mine.dropLast(".glance".count)) : mine
  }
  static var accessGroup: String { "group.\(appBundleId)" }

  /// expo-secure-store writes an unauthenticated item under service
  /// "app:no-auth" with the key as both account and generic attribute.
  static func read(_ key: String) -> String? {
    let keyData = Data(key.utf8)
    let query: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: "app:no-auth",
      kSecAttrGeneric as String: keyData,
      kSecAttrAccount as String: keyData,
      kSecAttrAccessGroup as String: accessGroup,
      kSecReturnData as String: true,
      kSecMatchLimit as String: kSecMatchLimitOne,
    ]
    var item: CFTypeRef?
    guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
          let data = item as? Data else { return nil }
    return String(data: data, encoding: .utf8)
  }

  static var credential: (url: String, token: String)? {
    guard let url = read("oikonome.widgetServerUrl"),
          let token = read("oikonome.widgetToken"),
          !url.isEmpty, !token.isEmpty else { return nil }
    return (url, token)
  }
}

// MARK: - the timeline

struct Entry: TimelineEntry {
  let date: Date
  let signedIn: Bool
  let glance: Glance?
  let fetchedAt: Date?
}

let staleAfter: TimeInterval = 60 * 60
let refreshEvery: TimeInterval = 30 * 60

struct Provider: TimelineProvider {
  func placeholder(in context: Context) -> Entry {
    Entry(date: .now, signedIn: true, glance: Glance.sample, fetchedAt: .now)
  }

  func getSnapshot(in context: Context, completion: @escaping (Entry) -> Void) {
    if context.isPreview { completion(placeholder(in: context)); return }
    load { completion($0) }
  }

  func getTimeline(in context: Context, completion: @escaping (Timeline<Entry>) -> Void) {
    load { entry in
      completion(Timeline(entries: [entry], policy: .after(.now + refreshEvery)))
    }
  }

  /// Fetch with the widget credential. A 401 means the credential is
  /// gone (revoked device, password change) — the widget says "Sign in"
  /// until the app next opens and mints again. Any other failure keeps
  /// the last payload: stale beats wrong, wrong beats blank. The payload
  /// is one household's numbers, so it goes with the credential: no
  /// credential or a refused one drops it, and the app clears it itself
  /// before writing another household's credential.
  private func load(_ done: @escaping (Entry) -> Void) {
    guard let cred = Shared.credential else {
      Cache.clear()
      done(Entry(date: .now, signedIn: false, glance: nil, fetchedAt: nil)); return
    }
    guard let url = URL(string: cred.url + "/api/today/glance") else {
      done(Entry(date: .now, signedIn: true, glance: nil, fetchedAt: nil)); return
    }
    var req = URLRequest(url: url, timeoutInterval: 20)
    req.setValue("Bearer \(cred.token)", forHTTPHeaderField: "Authorization")
    req.setValue("application/json", forHTTPHeaderField: "Accept")
    URLSession.shared.dataTask(with: req) { data, resp, _ in
      let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
      if status == 401 {
        Cache.clear()
        done(Entry(date: .now, signedIn: false, glance: nil, fetchedAt: nil)); return
      }
      if status == 200, let data, let g = try? JSONDecoder().decode(Glance.self, from: data) {
        // the app may have signed out or minted for another household
        // while this was in flight: then these are the previous
        // credential's numbers, never saved or drawn under the new one
        if Cache.save(g, fetchedWith: cred.token) {
          done(Entry(date: .now, signedIn: true, glance: g, fetchedAt: .now)); return
        }
      }
      // re-read: a sign-out while this was in flight draws "Sign in"
      let cached = Cache.load()
      done(Entry(date: .now, signedIn: Shared.credential != nil,
                 glance: cached?.0, fetchedAt: cached?.1))
    }.resume()
  }
}

/// The last payload, in the app group's defaults, so a redraw with no
/// network still shows something honest (grey, with its time). The entry
/// carries a digest of the widget token it was fetched under ("glance_owner")
/// and is read back only while that token is still the credential, so a
/// fetch that lands after a sign-out or a change of household can never be
/// shown as the new household's day. The app removes the same keys
/// (modules/widget-reload, `clearCache`) at sign-out and on a change of
/// household — keep the names in step.
enum Cache {
  static var defaults: UserDefaults? { UserDefaults(suiteName: Shared.accessGroup) }
  /// A digest, not the token: the defaults are not the Keychain.
  static func stamp(_ token: String) -> String {
    SHA256.hash(data: Data(token.utf8)).map { String(format: "%02x", $0) }.joined()
  }
  /// Save only if the credential the fetch began with is still current.
  /// Returns whether it saved.
  @discardableResult
  static func save(_ g: Glance, fetchedWith token: String) -> Bool {
    guard Shared.credential?.token == token,
          let d = defaults, let data = try? JSONEncoder().encode(g) else { return false }
    d.set(data, forKey: "glance"); d.set(Date.now.timeIntervalSince1970, forKey: "glance_at")
    d.set(stamp(token), forKey: "glance_owner")
    return true
  }
  /// The payload, only if it was fetched under the current credential; an
  /// entry with no stamp (an older build) is nobody's.
  static func load() -> (Glance, Date)? {
    guard let token = Shared.credential?.token, let d = defaults,
          d.string(forKey: "glance_owner") == stamp(token),
          let data = d.data(forKey: "glance"),
          let g = try? JSONDecoder().decode(Glance.self, from: data) else { return nil }
    return (g, Date(timeIntervalSince1970: d.double(forKey: "glance_at")))
  }
  static func clear() {
    for k in ["glance", "glance_at", "glance_owner"] { defaults?.removeObject(forKey: k) }
  }
}

// MARK: - colours: the app's palettes (src/lib/theme.ts), light and dark

struct Palette {
  let card, text, mut, good, bad, track: Color
  static let dark = Palette(card: Color(hex: 0x1a212b), text: Color(hex: 0xdbe3ea),
                            mut: Color(hex: 0x8b98a3), good: Color(hex: 0x5cb56b),
                            bad: Color(hex: 0xe0695d), track: Color(hex: 0x2a3441))
  static let light = Palette(card: .white, text: Color(hex: 0x1f2933),
                             mut: Color(hex: 0x5b6875), good: Color(hex: 0x2f8f46),
                             bad: Color(hex: 0xc44b3e), track: Color(hex: 0xe3e8ee))
}

extension Color {
  init(hex: UInt32) {
    self.init(red: Double((hex >> 16) & 0xff) / 255,
              green: Double((hex >> 8) & 0xff) / 255,
              blue: Double(hex & 0xff) / 255)
  }
}

// MARK: - the face (mirrors glance-pure.glanceFace)

func dollars(_ n: Double) -> String {
  let f = NumberFormatter(); f.numberStyle = .decimal; f.maximumFractionDigits = 0
  return "$" + (f.string(from: NSNumber(value: abs(n).rounded())) ?? "0")
}

func heroLabel(_ verdict: String?) -> String {
  switch verdict {
  case "OVER BUDGET": return "Over budget"
  case "UNDER BUDGET": return "Under budget"
  default: return "On budget"
  }
}

func clockLabel(_ d: Date) -> String {
  let f = DateFormatter(); f.dateFormat = "h:mm a"; return f.string(from: d)
}

struct GlanceView: View {
  let entry: Entry
  @Environment(\.colorScheme) var scheme
  @Environment(\.widgetFamily) var family

  var p: Palette { scheme == .dark ? .dark : .light }

  var body: some View {
    Group {
      if !entry.signedIn {
        plain("Sign in", "to see what's left today")
      } else if let g = entry.glance, !g.budgets_set {
        plain("Set a plan", "the widget needs a monthly budget")
      } else if let g = entry.glance {
        verdict(g)
      } else {
        plain("…", "refreshing")
      }
    }
    .widgetURL(URL(string: "oikonome://"))
    .containerBackground(p.card, for: .widget)
  }

  func plain(_ title: String, _ sub: String) -> some View {
    VStack(alignment: .leading, spacing: 4) {
      Text("OIKONOME").font(.system(size: 11, weight: .semibold)).foregroundStyle(p.mut)
        .tracking(0.6)
      Text(title).font(.system(size: 22, weight: .heavy)).foregroundStyle(p.text)
        .padding(.top, 6)
      Text(sub).font(.system(size: 12)).foregroundStyle(p.mut)
    }
    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
  }

  func verdict(_ g: Glance) -> some View {
    let fetched = entry.fetchedAt ?? .distantPast
    let stale = Date.now.timeIntervalSince(fetched) > staleAfter
    let allow = g.day_allow ?? 0, spent = g.day_spent ?? 0
    let over = spent > allow + 0.5
    let fill = over ? 1.0 : allow > 0 ? min(1.0, spent / allow) : 0.0
    let sub = over ? "\(dollars(spent - allow)) over · \(g.days_left ?? 0) days left"
                   : "\(dollars(spent)) of \(dollars(allow)) spent"
    let tone = stale ? p.mut : g.verdict == "OVER BUDGET" ? p.bad : p.good
    let chips = (g.chips ?? []).prefix(4)
    return HStack(alignment: .top, spacing: 14) {
      VStack(alignment: .leading, spacing: 2) {
        Text("LEFT TODAY").font(.system(size: 11, weight: .semibold)).foregroundStyle(p.mut)
          .tracking(0.6)
        Text(dollars(g.left_today ?? 0))
          .font(.system(size: family == .systemMedium ? 40 : 36, weight: .heavy))
          .foregroundStyle(stale ? p.mut : p.text)
          .minimumScaleFactor(0.6).lineLimit(1)
          .padding(.top, 4)
        Text(stale ? "as of \(clockLabel(fetched))" : heroLabel(g.verdict))
          .font(.system(size: 14, weight: .heavy)).foregroundStyle(tone).lineLimit(1)
        GeometryReader { geo in
          ZStack(alignment: .leading) {
            Capsule().fill(p.track)
            Capsule().fill(stale ? p.mut : over ? p.bad : p.good)
              .frame(width: geo.size.width * fill)
          }
        }
        .frame(height: 6).padding(.top, 8)
        Text(stale ? "tap to refresh" : sub)
          .font(.system(size: 12)).foregroundStyle(p.mut).lineLimit(1)
          .padding(.top, 4)
      }
      if family == .systemMedium && !chips.isEmpty {
        VStack(alignment: .leading, spacing: 4) {
          ForEach(Array(chips.enumerated()), id: \.offset) { _, c in
            Text(c.text).font(.system(size: 12))
              .foregroundStyle(c.tone == "neg" ? p.bad : p.text).lineLimit(1)
          }
        }
        .frame(width: 130, alignment: .leading)
        .frame(maxHeight: .infinity, alignment: .center)
      }
    }
    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
  }
}

extension Glance {
  /// The widget gallery's preview: invented, whole-dollar, on plan.
  static let sample = Glance(
    date: "2026-01-15", budgets_set: true, verdict: "ON BUDGET",
    left_today: 47, day_spent: 29, day_allow: 76, days_left: 16,
    pace_line: "$12/day ahead",
    chips: [Chip(text: "Groceries $22 today", tone: ""),
            Chip(text: "Everything else $25 today", tone: "")],
    as_of: "2026-01-15T09:40:00Z")
}

@main
struct GlanceWidget: Widget {
  var body: some WidgetConfiguration {
    StaticConfiguration(kind: "glance", provider: Provider()) { entry in
      GlanceView(entry: entry)
    }
    .configurationDisplayName("Left today")
    .description("What's still spendable today, and the day's verdict.")
    .supportedFamilies([.systemSmall, .systemMedium])
  }
}
