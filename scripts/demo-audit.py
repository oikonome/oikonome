"""Demo-data audit: is the seeded demo REALISTIC and internally consistent?

Run it against a freshly seeded demo database after any change to demo.py —
the generator is easy to change in a way that reads fine in code and produces
nonsense on screen (a six-figure balance in a side business's checking
account, an equity page that disagrees with the ledger beside it, receipts
whose line items don't sum to the charge).

    OIKONOME_ADMIN_DSN=... OIKONOME_DSN=... OIKONOME_MASTER_KEY=... \
        python scripts/demo-audit.py

Checks: dates/ids · sign conventions · balances vs ledger · household income
· business realism · owner-draw double entry · equity vs ledger · entity
assignment · receipts · notes · investments vs holdings · loans · bills ·
plan vs actual (via the app's own month_status) · the capital-account
identity · and that the Today view and Schedule-C P&L actually build on the
seeded data. Exits non-zero on any FAIL.

The checks are worth running against every reseed: the demo is the only
place a whole household's worth of interlocking records is generated at
once, so an engine bug that no single-behaviour test reaches (unbounded
business float, equity that ignores retained earnings) shows up here first.
"""
import sys
import datetime as dt
from collections import defaultdict
from oikonome.db import tenancy

a = tenancy.admin_connect()
tid = str(a.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]); a.close()
c = tenancy.tenant_connect(tid)
q = lambda s, p=(): c.execute(s, p).fetchall()
one = lambda s, p=(): q(s, p)[0]
fails, warns = [], []
def chk(cond, msg, hard=True):
    if cond: print(f"  ok   {msg}")
    else:
        (fails if hard else warns).append(msg)
        print(f"  {'FAIL' if hard else 'WARN'} {msg}")

today = dt.date.today()
print("\n== 1. dates & ids ==")
fut = one("SELECT count(*) n FROM transactions WHERE date > current_date")["n"]
chk(fut == 0, f"no future-dated transactions (found {fut})")
dup = one("SELECT count(*) n FROM (SELECT id FROM transactions GROUP BY id HAVING count(*)>1) x")["n"]
chk(dup == 0, f"no duplicate transaction ids ({dup})")
oldest = one("SELECT min(date) d, max(date) x, count(*) n FROM transactions")
print(f"       span {oldest['d']} → {oldest['x']}, {oldest['n']:,} txns")

print("\n== 2. sign conventions ==")
badinc = one("SELECT count(*) n FROM transactions WHERE category_primary='INCOME' AND amount>0")["n"]
chk(badinc == 0, f"INCOME rows are negative (money in) — {badinc} wrong-signed")
for acct in ("demo-chk", "demo-biz"):
    neg = one("SELECT count(*) n FROM transactions WHERE account_id=%s AND amount<0", (acct,))["n"]
    print(f"       {acct}: {neg} inflow rows")

print("\n== 3. account balances vs ledger ==")
for r in q("SELECT id, name, type, balance_current FROM accounts ORDER BY id"):
    flow = one("SELECT coalesce(-sum(amount),0) v FROM transactions WHERE account_id=%s", (r["id"],))["v"]
    print(f"       {r['id']:<10} {r['name'][:26]:<27} bal {float(r['balance_current'] or 0):>12,.2f}  net-flow {float(flow):>12,.2f}")

print("\n== 4. household income realism ==")
for y in (today.year - 2, today.year - 1):
    pay = one("SELECT coalesce(-round(sum(amount)),0) v FROM transactions WHERE category_primary='INCOME' AND account_id='demo-chk' AND extract(year from date)=%s", (y,))["v"]
    chk(60_000 <= float(pay) <= 160_000, f"{y} take-home ${float(pay):,.0f} in a believable band")
ia = q("SELECT year, wages FROM income_annual ORDER BY year DESC LIMIT 3")
print("       income_annual:", [(r["year"], float(r["wages"])) for r in ia])

print("\n== 5. business realism ==")
ent = one("SELECT name, ein_last4, structure, formation_date, business_start_date FROM business_entity")
print(f"       {ent['name']} · {ent['structure']} · EIN ****{ent['ein_last4']} · formed {ent['formation_date']}")
chk(ent["business_start_date"] >= ent["formation_date"], "business start is on/after formation")
years = sorted({r["y"] for r in q("SELECT DISTINCT extract(year from date)::int y FROM transactions WHERE account_id='demo-biz'")})
for y in years:
    rev = float(one("SELECT coalesce(-sum(amount),0) v FROM transactions WHERE account_id='demo-biz' AND amount<0 AND category_primary='INCOME' AND extract(year from date)=%s", (y,))["v"])
    exp = float(one("SELECT coalesce(sum(amount),0) v FROM transactions WHERE account_id='demo-biz' AND amount>0 AND name<>'OWNER DRAW' AND extract(year from date)=%s", (y,))["v"])
    draw = float(one("SELECT coalesce(sum(amount),0) v FROM transactions WHERE account_id='demo-biz' AND name='OWNER DRAW' AND extract(year from date)=%s", (y,))["v"])
    full = y not in (min(years), today.year)
    print(f"       {y}: revenue ${rev:>10,.0f}  expenses ${exp:>9,.0f}  net ${rev-exp:>10,.0f}  draws ${draw:>9,.0f}")
    if full:
        chk(15_000 <= rev <= 70_000, f"{y} revenue reads as a side practice", hard=False)
        # NOT "draws <= this year's net": a loss-making month draws nothing
        # yet still lowers the annual net, so the yearly comparison is not an
        # invariant. The real one is cumulative and checked below.
        chk(draw <= (rev - exp) * 1.35 + 2000,
            f"{y} draws stay near that year's profit", hard=False)
bal = float(one("SELECT balance_current v FROM accounts WHERE id='demo-biz'")["v"])
chk(1_000 <= bal <= 40_000, f"business float ${bal:,.0f} is a plausible working balance")

print("\n== 6. owner-draw double entry ==")
d_out = float(one("SELECT coalesce(sum(amount),0) v FROM transactions WHERE account_id='demo-biz' AND name='OWNER DRAW'")["v"])
d_in = float(one("SELECT coalesce(-sum(amount),0) v FROM transactions WHERE account_id='demo-chk' AND name LIKE 'TRANSFER FROM FERNWOOD%%'")["v"])
chk(abs(d_out - d_in) < 0.02, f"every draw out of the business lands in checking (${d_out:,.2f} vs ${d_in:,.2f})")

print("\n== 7. equity vs ledger ==")
em = {r["kind"]: float(r["t"]) for r in q("SELECT kind, sum(amount) t FROM equity_movement GROUP BY kind")}
print("       ", {k: round(v, 2) for k, v in em.items()})
led_draws = float(one("SELECT coalesce(sum(amount),0) v FROM transactions WHERE account_id='demo-biz' AND name='OWNER DRAW' AND id IN (SELECT txn_id FROM equity_movement WHERE txn_id IS NOT NULL)")["v"])
chk(abs(em.get("draw", 0) - led_draws) < 0.02, "recorded draws match their ledger rows exactly")
orph = one("SELECT count(*) n FROM equity_movement e WHERE e.txn_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.id=e.txn_id)")["n"]
chk(orph == 0, f"no equity movement points at a missing transaction ({orph})")

print("\n== 8. entity assignment ==")
chk(one("SELECT count(*) n FROM accounts WHERE id='demo-biz' AND entity_id IS NOT NULL")["n"] == 1, "business account is entity-owned")
ov = one("SELECT count(*) n FROM transactions WHERE entity_id IS NOT NULL AND account_id<>'demo-biz'")["n"]
chk(ov > 0, f"personal-card business costs use the per-txn override ({ov})")
bad = one("SELECT count(*) n FROM transactions t WHERE t.entity_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM business_entity e WHERE e.id=t.entity_id)")["n"]
chk(bad == 0, f"no transaction points at a missing entity ({bad})")

print("\n== 9. receipts ==")
rc = one("SELECT count(*) n FROM receipts")["n"]
ri = one("SELECT count(*) n FROM receipt_items")["n"]
chk(rc >= 15, f"{rc} receipts seeded (items {ri})")
mism = q("""SELECT r.id, t.amount, round(sum(i.amount)::numeric,2) s
            FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
            JOIN transactions t ON t.id=r.txn_id GROUP BY r.id, t.amount
            HAVING abs(round(sum(i.amount)::numeric,2) - t.amount::numeric) > 0.02""")
chk(not mism, f"every receipt's line items sum to its transaction ({len(mism)} mismatched)")
orphan_r = one("SELECT count(*) n FROM receipts r WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.id=r.txn_id)")["n"]
chk(orphan_r == 0, f"no receipt without its transaction ({orphan_r})")

print("\n== 10. notes ==")
nn = one("SELECT count(*) n FROM transaction_notes")["n"]
chk(nn >= 30, f"{nn} notes seeded")
orphan_n = one("SELECT count(*) n FROM transaction_notes x WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.id=x.txn_id)")["n"]
chk(orphan_n == 0, f"no note without its transaction ({orphan_n})")
blank = one("SELECT count(*) n FROM transaction_notes WHERE btrim(note)=''")["n"]
chk(blank == 0, f"no empty notes ({blank})")
unformatted = one("SELECT count(*) n FROM transaction_notes WHERE note LIKE '%%{n}%%'")["n"]
chk(unformatted == 0, f"no unformatted placeholders left in notes ({unformatted})")

print("\n== 11. investments ==")
for r in q("""SELECT a.id, a.name, a.balance_current bal,
                     coalesce(sum(h.quantity * h.price),0) mv
              FROM accounts a LEFT JOIN holdings h ON h.account_id=a.id
              WHERE a.type='investment' GROUP BY a.id, a.name, a.balance_current"""):
    d = abs(float(r["bal"] or 0) - float(r["mv"] or 0))
    chk(d < max(1.0, float(r["bal"] or 0) * 0.01),
        f"{r['name']}: balance ${float(r['bal']):,.0f} matches holdings ${float(r['mv']):,.0f}")

print("\n== 12. loans ==")
for aid, nm in (("demo-mort", "mortgage"), ("demo-auto", "auto")):
    r = one("SELECT balance_current v FROM accounts WHERE id=%s", (aid,))
    chk(float(r["v"]) > 0, f"{nm} balance positive (${float(r['v']):,.0f})")

print("\n== 13. bills ==")
bl = q("SELECT payee, amount, frequency, due_on, active FROM bills WHERE coalesce(active,1)=1 ORDER BY due_on")
chk(len(bl) >= 5, f"{len(bl)} active bills configured")
stale = [b for b in bl if b["due_on"] and b["due_on"] < today - dt.timedelta(days=40)]
chk(not stale, f"no active bill is stuck >40d in the past ({len(stale)})")
zero = [b for b in bl if not b["amount"]]
chk(not zero, f"no bill has a zero/blank amount ({len(zero)})")
# each bill's amount should resemble what the ledger actually pays it
drift = []
for b in bl:
    if not b["payee"]:
        continue
    r = one("""SELECT round(avg(abs(amount))::numeric,2) v, count(*) n FROM transactions
               WHERE upper(coalesce(merchant_name,name)) LIKE upper(%s)
                 AND date >= current_date - interval '400 days'""",
            (f"%{b['payee'][:14]}%",))
    if r["n"] and r["v"] and abs(b["amount"]):
        ratio = float(r["v"]) / abs(float(b["amount"]))
        if not (0.5 <= ratio <= 2.0):
            drift.append(f"{b['payee']}: bill ${abs(float(b['amount'])):,.0f} vs ledger avg ${float(r['v']):,.0f}")
chk(not drift, "bill amounts track the ledger: " + ("; ".join(drift) if drift else "all within 2x"), hard=False)

print("\n== 14. plan vs actual, using the APP's own math ==")
from oikonome.engine import budget as _b
cfg = _b.load_config(c)
food, other = float(cfg.get("food_monthly") or 0), float(cfg.get("other_monthly") or 0)
st = _b.month_status(c, today)
spent = float(st.get("variable_actual") or 0)
plan = float(st.get("variable_expected") or (food + other))
dim = today.day
print(f"       month-to-date variable spend ${spent:,.0f} vs expected-to-date "
      f"${plan:,.0f} (day {dim}) · verdict {st.get('verdict')}")
pace = spent
print(f"       implied full-month pace ${pace:,.0f}")
chk(0.4 <= pace / max(plan, 1) <= 2.2,
    f"variable spend tracks the expected pace ({pace/max(plan,1):.2f}x)", hard=False)
inc = float(_b.monthly_income(c) or 0)
print(f"       monthly income (engine): ${inc:,.0f}")
chk(inc > plan, "income exceeds the variable-spend plan")

print("\n== 14b. capital account (the cumulative invariant) ==")
from oikonome.engine import equity as _eq
_eid = one("SELECT id FROM business_entity")["id"]
cap = _eq.capital_summary(c, _eid)
print(f"       contributions ${cap['contributions']:,.0f} + retained ${cap['net_income']:,.0f}"
      f" − draws ${cap['draws']:,.0f} = ${cap['capital_balance']:,.0f}")
chk(abs(cap["contributions"] + cap["net_income"] - cap["draws"]
        - cap["distributions"] - cap["capital_balance"]) < 0.02,
    "capital identity holds")
chk(cap["capital_balance"] > 0,
    f"owner has not drawn past contributions + earnings (${cap['capital_balance']:,.0f})")

print("\n== 15. engine runs on the seeded data ==")
try:
    from oikonome.web import report, todayview
    # the app's own path: report.gather() produces what build_context wants
    gathered = report.gather(c, today, live=True)
    ctx = todayview.build_context(gathered)
    chk(bool(ctx), f"Today/verdict view builds ({len(ctx)} keys) — "
                   f"verdict {gathered.get('verdict')}")
except Exception as e:
    chk(False, f"Today view raised: {type(e).__name__}: {e}")
try:
    from oikonome.engine import books
    eid = one("SELECT id FROM business_entity")["id"]
    pl = books.pnl(c, eid, today.year)
    rev = float(pl.get("revenue") or 0)
    exp = float(pl.get("operating_expenses") or 0)
    print(f"       P&L {today.year}: revenue ${rev:,.0f} expenses ${exp:,.0f} net ${rev-exp:,.0f}")
    chk(rev > 0 and exp > 0, "Schedule-C P&L engine produces non-empty figures")
except Exception as e:
    chk(False, f"books engine raised: {type(e).__name__}: {e}")

print("\n== summary ==")
print(f"  FAIL {len(fails)} · WARN {len(warns)}")
for f in fails: print("   FAIL:", f)
for w in warns: print("   WARN:", w)
c.close()
sys.exit(1 if fails else 0)
