// The iOS home-screen widget: the Today hero's simple face, and nothing
// past it. The extension fetches for itself — the app's device token is
// behind the biometric gate, so the widget polls with its own lesser
// token, read from the Keychain in the app group the app shares with it
// (src/lib/glance.ts writes it there through expo-secure-store). Every
// string is composed by the server; this file mirrors the decisions in
// src/lib/glance-pure.ts — stale after an hour or once the local day has
// turned since the fetch, "Sign in" without a credential, "Set a plan" before a budget exists — line for line.
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
  static func query(_ key: String) -> [String: Any] {
    let keyData = Data(key.utf8)
    return [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: "app:no-auth",
      kSecAttrGeneric as String: keyData,
      kSecAttrAccount as String: keyData,
      kSecAttrAccessGroup as String: accessGroup,
    ]
  }

  static func read(_ key: String) -> String? {
    var q = query(key)
    q[kSecReturnData as String] = true
    q[kSecMatchLimit as String] = kSecMatchLimitOne
    var item: CFTypeRef?
    guard SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess,
          let data = item as? Data else { return nil }
    return String(data: data, encoding: .utf8)
  }

  /// Written the way expo-secure-store writes, so the app can delete it by
  /// name. Delete-then-add, because an update keeps an existing item's
  /// accessibility: after first unlock (a timeline reloads while the phone
  /// is locked) and this device only, like every item the app keeps.
  static func write(_ key: String, _ value: Data) {
    SecItemDelete(query(key) as CFDictionary)
    var item = query(key)
    item[kSecValueData as String] = value
    item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
    SecItemAdd(item as CFDictionary, nil)
  }

  static func delete(_ key: String) {
    SecItemDelete(query(key) as CFDictionary)
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

/// Stale beats wrong: past an hour, or fetched on an earlier local day —
/// the glance is one day's allowance, so last night's "$0 left" is wrong
/// at 12:30 AM however young it is (glance-pure.sameLocalDay).
func isStale(fetched: Date, at now: Date) -> Bool {
  now.timeIntervalSince(fetched) > staleAfter
    || !Calendar.current.isDate(fetched, inSameDayAs: now)
}

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
      // A face is decided per entry, so the day turning needs an entry of
      // its own: at the next local midnight the same payload redraws grey,
      // even if the system holds back the next reload.
      // The hour rule needs the same: an entry just past an hour after the
      // fetch draws the payload grey if no reload has come by then.
      var entries = [entry]
      let cal = Calendar.current
      if entry.glance != nil,
         let midnight = cal.date(byAdding: .day, value: 1, to: cal.startOfDay(for: entry.date)) {
        entries.append(Entry(date: midnight, signedIn: entry.signedIn,
                             glance: entry.glance, fetchedAt: entry.fetchedAt))
        if let fetched = entry.fetchedAt {
          let hourOld = fetched.addingTimeInterval(staleAfter + 1)
          if hourOld > entry.date && hourOld < midnight {
            entries.append(Entry(date: hourOld, signedIn: entry.signedIn,
                                 glance: entry.glance, fetchedAt: entry.fetchedAt))
          }
        }
        entries.sort { $0.date < $1.date }
      }
      completion(Timeline(entries: entries, policy: .after(.now + refreshEvery)))
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
    var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData,
                         timeoutInterval: 20)
    req.setValue("Bearer \(cred.token)", forHTTPHeaderField: "Authorization")
    req.setValue("application/json", forHTTPHeaderField: "Accept")
    // no-store: kept out of any HTTP disk cache, as in the app (api.ts)
    req.setValue("no-store", forHTTPHeaderField: "Cache-Control")
    // older builds fetched through the shared session, whose disk cache
    // kept the response and the request with its bearer header
    URLCache.shared.removeAllCachedResponses()
    // when the request went out, not when it landed: a fetch sent before
    // midnight carries the day that was ending, so landing after it must
    // read as yesterday's (grey), never as today's allowance
    let started = Date.now
    glanceSession.dataTask(with: req) { data, resp, _ in
      let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
      // a refusal is about the credential this request carried; if the app
      // has minted another since, that one and its payload are left alone
      if status == 401, Shared.credential?.token == cred.token {
        Cache.clear()
        done(Entry(date: .now, signedIn: false, glance: nil, fetchedAt: nil)); return
      }
      if status == 200, let data, let g = try? JSONDecoder().decode(Glance.self, from: data) {
        // the app may have signed out or minted for another household
        // while this was in flight: then these are the previous
        // credential's numbers, never saved or drawn under the new one
        if Cache.save(g, fetchedWith: cred.token, at: started) {
          done(Entry(date: .now, signedIn: true, glance: g, fetchedAt: started)); return
        }
      }
      // re-read: a sign-out while this was in flight draws "Sign in"
      let cached = Cache.load()
      done(Entry(date: .now, signedIn: Shared.credential != nil,
                 glance: cached?.0, fetchedAt: cached?.1))
    }.resume()
  }
}

/// The extension's network session: ephemeral, with no URL cache, so
/// neither the household's numbers nor the request carrying the widget's
/// bearer token are ever written to a disk cache.
let glanceSession: URLSession = {
  let c = URLSessionConfiguration.ephemeral
  c.urlCache = nil
  c.requestCachePolicy = .reloadIgnoringLocalCacheData
  return URLSession(configuration: c)
}()

/// The last payload, so a redraw with no network still shows something
/// honest (grey, with its time). It is one household's financial day, so it
/// lives in the Keychain beside the credential (shared group, this device
/// only, kept off backups) rather than in a defaults plist. The entry
/// carries a digest of the widget token it was fetched under and is read
/// back only while that token is still the credential, so a fetch that
/// lands after a sign-out or a change of household can never be shown as
/// the new household's day. The app deletes the same item by name
/// (src/lib/glance.ts, `dropGlanceCaches`) at sign-out and on a change of
/// household — keep the name in step.
enum Cache {
  static let key = "oikonome.widgetGlanceExt"
  struct Stored: Codable { let glance: Glance; let at: Double; let owner: String }
  /// A digest, not the token: equality is all the entry needs.
  static func stamp(_ token: String) -> String {
    SHA256.hash(data: Data(token.utf8)).map { String(format: "%02x", $0) }.joined()
  }
  /// Save only if the credential the fetch began with is still current.
  /// Returns whether it was (and so whether the payload may be drawn).
  @discardableResult
  static func save(_ g: Glance, fetchedWith token: String, at started: Date) -> Bool {
    dropLegacy()
    guard Shared.credential?.token == token else { return false }
    let s = Stored(glance: g, at: started.timeIntervalSince1970, owner: stamp(token))
    if let data = try? JSONEncoder().encode(s) { Shared.write(key, data) }
    return true
  }
  /// The payload, only if it was fetched under the current credential.
  static func load() -> (Glance, Date)? {
    dropLegacy()
    guard let token = Shared.credential?.token, let raw = Shared.read(key),
          let s = try? JSONDecoder().decode(Stored.self, from: Data(raw.utf8)),
          s.owner == stamp(token) else { return nil }
    return (s.glance, Date(timeIntervalSince1970: s.at))
  }
  static func clear() {
    Shared.delete(key)
    dropLegacy()
  }
  /// Older builds kept the payload in the app group's defaults, a plist
  /// that device backups carry; whatever is left there is removed.
  static func dropLegacy() {
    guard let d = UserDefaults(suiteName: Shared.accessGroup) else { return }
    for k in ["glance", "glance_at", "glance_owner"] { d.removeObject(forKey: k) }
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
    // judged at the entry's own date, not the wall clock: WidgetKit may
    // render a future entry (the midnight one) before its time comes
    let stale = isStale(fetched: fetched, at: entry.date)
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
