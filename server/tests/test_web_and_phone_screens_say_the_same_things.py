"""The phone's screens say what the web's pages say, in the web's words,
and print the server's figures without working out their own.

Read from the client SOURCES: the behaviour sits in render code no unit
test can call. Each test names one thing a reader
of both clients would see differ, and fails if either side drifts.
"""

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


WEB = "webapp/src/pages/"
PHONE = "mobile/src/app/"


class AFailedRefetchSaysSoOnBothClients(unittest.TestCase):
    def test_every_listed_web_page_carries_the_stale_line(self):
        for page in ["NetWorth.tsx", "MonthLens.tsx", "YearLens.tsx",
                     "Bills.tsx"]:
            self.assertIn("<StaleLine query={q} />", src(WEB + page), page)


class NetWorth(unittest.TestCase):
    phone = src(PHONE + "networth.tsx")

    def test_a_breakdown_prints_the_servers_shown_rows_when_sent(self):
        # the raw rows stand in only for a server that sends no `shown`
        self.assertIn("const drawn = shown\n    ? shown.rows", self.phone)
        self.assertIn("older-server path: stands in for `rings.*.shown`",
                      self.phone)

    def test_an_owed_row_is_named_as_the_web_names_it(self):
        self.assertIn("owed · not drawn", src("webapp/src/components/Donut.tsx"))
        self.assertIn("· owed · not drawn", self.phone)

    def test_the_tables_use_the_webs_column_names(self):
        for head in ["Cash in (bank)", "Value now", "Return"]:
            self.assertIn(f">{head}</Text>", self.phone, head)
        self.assertNotIn(">Ret.</Text>", self.phone)

    def test_every_year_of_fees_is_listed(self):
        self.assertNotIn(".reverse().slice(0, 20)", self.phone)

    def test_the_financial_tile_links_to_the_payoff_plan(self):
        self.assertIn('router.push("/debt")', self.phone)

    def test_the_property_card_stands_with_no_items(self):
        self.assertNotIn("d.property_items.length === 0 && <Card><ManualAssets />",
                         self.phone)

    def test_a_retirement_total_with_no_return_prints_no_dash(self):
        self.assertNotIn("{plTxt(rtot.pl)} · {pctTxt(rtot.pct)}", self.phone)

    def test_the_value_that_stopped_a_save_is_marked(self):
        self.assertIn("setErrAt(j);", self.phone)
        self.assertIn("errAt === i && { borderColor: C.bad }", self.phone)


class CashFlow(unittest.TestCase):
    phone = src(PHONE + "cashflow.tsx")

    def test_the_paychecks_still_due_come_before_the_rate(self):
        due = self.phone.index("flow.income_due ? ` · ${flow.income_due.text}`")
        rate = self.phone.index("` · ${flow.rate}% of income`")
        self.assertLess(due, rate)

    def test_a_year_row_says_it_opens(self):
        self.assertIn("▸ open a year for its months", self.phone)

    def test_the_tabs_stand_while_the_overview_loads(self):
        self.assertNotIn("{!query.isPending && (\n        <Card>", self.phone)

    def test_interest_and_fees_print_the_servers_whole_dollars_first(self):
        self.assertIn("r.shown?.paid_ytd ?? r.paid_ytd", self.phone)
        self.assertIn("older-server path: stands in for `shown`", self.phone)

    def test_the_latest_charge_names_its_month(self):
        self.assertIn("({monYear(r.latest[2])})", self.phone)

    def test_the_records_follow_the_webs_order_and_words(self):
        reported = self.phone.index("d.reported_income.length > 0")
        funding = self.phone.index("<H>Investment funding by year</H>")
        self.assertLess(reported, funding)
        self.assertIn(">Total income</Text>", self.phone)
        self.assertIn("reported income (tax returns)", self.phone)

    def test_a_kind_or_a_month_reads_the_servers_figure_first(self):
        self.assertIn("w.kinds_shown?.[k] ?? v", self.phone)
        self.assertIn("d.months_by_year?.[y]\n", self.phone)


class Accounts(unittest.TestCase):
    phone = src(PHONE + "(tabs)/accounts.tsx")
    web = src(WEB + "Accounts.tsx")

    def test_the_web_never_guesses_a_connections_class(self):
        # the web is served by its own server, which always sends it; the
        # phone keeps the old guess for an older server, marked
        self.assertNotRegex(self.web, r"status_kind\s*\?\?\s*\(")
        self.assertIn("older-server path: stands in for `status_kind`",
                      self.phone)

    def test_a_cards_due_line_needs_its_date_as_on_the_web(self):
        self.assertIn("a.card_due && a.card_due_text ?", self.phone)

    def test_a_collector_says_when_it_last_ran(self):
        self.assertIn('"no successful run since"', self.phone)
        self.assertIn("token revoked ${seenDay(", self.phone)


class MonthLens(unittest.TestCase):
    phone = src(PHONE + "month.tsx")

    def test_unmeasured_income_is_the_servers_words_alone(self):
        self.assertIn("{d.income_text ? d.income_text\n", self.phone)
        self.assertIn("{!d.income_text && <>{\" → net \"}", self.phone)

    def test_a_month_with_no_rows_has_no_biggest_card(self):
        self.assertIn("{!noData && d.biggest.length === 0 && (", self.phone)

    def test_the_bills_card_is_titled_as_the_webs(self):
        self.assertIn("<H>Bills — paid vs planned</H>", self.phone)

    def test_the_amazon_row_opens_nothing(self):
        self.assertIn('onPress={key && name !== "Amazon"', self.phone)

    def test_dates_are_written_as_the_web_writes_them(self):
        self.assertNotIn("mmddyy(", self.phone)

    def test_no_step_runs_past_this_month(self):
        self.assertIn("ym.m >= now.getMonth() + 1", self.phone)


class YearLens(unittest.TestCase):
    phone = src(PHONE + "year.tsx")

    def test_a_net_cells_label_is_printed_as_sent(self):
        self.assertNotIn(".toUpperCase()", self.phone)
        self.assertNotIn('"On plan"', self.phone)

    def test_the_phone_jumps_to_any_year_as_the_web_dropdown_does(self):
        self.assertIn("choose a year", self.phone)

    def test_the_web_stops_at_the_first_year_as_the_phone_does(self):
        self.assertIn("y <= yearQ.data.first_year", src(WEB + "Home.tsx"))

    def test_a_categorys_change_carries_its_colour(self):
        self.assertIn("tone={yearChangeTone(d.changes?.categories[i])}",
                      self.phone)


class Merchants(unittest.TestCase):
    phone = src(PHONE + "merchants.tsx")
    detail = src(PHONE + "merchant.tsx")

    def test_an_undo_or_a_decision_says_what_it_did(self):
        for words in ['"Change undone."', '"Merged."', '"Kept apart."']:
            self.assertIn(words, self.phone, words)

    def test_the_demo_notice_is_the_webs(self):
        self.assertIn("Not available on the demo instance — synthetic data only.",
                      self.phone)

    def test_business_rows_are_named_rows(self):
        self.assertIn("business row${m.business_rows === 1", self.phone)

    def test_the_detail_opens_the_merchants_rows(self):
        self.assertIn("open in Transactions →", self.detail)

    def test_a_place_the_maps_cannot_find_has_no_map_link(self):
        self.assertIn("(l.lat != null || l.address || l.city || l.postal)",
                      self.detail)


class BusinessTax(unittest.TestCase):
    web = src(WEB + "Business.tsx")
    phone = src(PHONE + "business.tsx")

    def test_both_print_the_servers_w2_wages_sentence_under_the_se_tax(self):
        self.assertIn("{e.ss_wages_note && (", self.web)
        self.assertIn("{e.ss_wages_note}</span>)}", self.web)
        self.assertIn("{e0?.ss_wages_note\n", self.phone)
        self.assertIn("<Text style={s.mut}>{e0.ss_wages_note}</Text>",
                      self.phone)
        # below the loss note on both, as the web places it
        self.assertLess(self.web.index("{e.loss_note && ("),
                        self.web.index("{e.ss_wages_note && ("))
        self.assertLess(self.phone.index("{e0?.loss_note ?"),
                        self.phone.index("{e0?.ss_wages_note"))

    def test_both_offer_whose_business_with_the_servers_labels(self):
        self.assertIn("const owners = entity.owner_options ?? [];", self.web)
        self.assertIn("const owners = entity?.owner_options ?? [];",
                      self.phone)
        for side in (self.web, self.phone):
            self.assertIn("{o.label}", side)
            self.assertIn("whose W-2 wages the self-employment tax counts",
                          side.replace("Whose W-2", "whose W-2"))
        # an older server sends no choices, and nothing is sent back
        self.assertIn("if (owners.length) body.owner = f.owner;", self.web)
        self.assertIn("...(owners.length ? { owner } : {}),", self.phone)


class Budget(unittest.TestCase):
    phone = src(PHONE + "budget.tsx")

    def test_unknown_bills_never_print_as_zero(self):
        self.assertNotIn("d.avg_bills ?? 0", self.phone)

    def test_the_scaled_figure_is_the_paced_one_alone(self):
        self.assertIn("(scaled to ${money(d.variable_budget_paced ?? d.variable_budget, false)}",
                      self.phone)

    def test_the_planner_says_what_the_web_planner_says(self):
        for words in ["go Back to adjust",
                      "Smart categorization may still be running",
                      "a merchant match wins",
                      "Suggestions are the median of the last {suggMonths} months.",
                      "auto-shrinks as categories",
                      "— trip fund, emergency fund; progress comes from real transfers"]:
            self.assertIn(words, self.phone, words)

    def test_a_take_home_field_is_not_called_monthly(self):
        self.assertNotIn("take-home per month`)", self.phone)

    def test_a_suggested_category_reads_as_money(self):
        self.assertIn("~{m$(c.monthly)}/mo", self.phone)

    def test_food_can_be_removed_but_its_name_is_fixed_on_both(self):
        # no save carries a Food name, so neither client offers to edit it;
        # the remove control stays, as on the web
        self.assertIn("editable={!viewer && !r.food}", self.phone)
        self.assertIn("readOnly={!!r.food}",
                      src(WEB.replace("pages/", "components/")
                          + "BudgetPlanner.tsx"))
        self.assertNotIn("{!viewer && !r.food && (", self.phone)

    def test_money_out_is_drawn_in_its_parts(self):
        self.assertIn("<SegBar label={`out ", self.phone)


class Spending(unittest.TestCase):
    def test_a_failed_records_read_says_so_on_both_clients(self):
        for f in [WEB + "Spending.tsx", PHONE + "spending.tsx"]:
            self.assertIn('what="the spending records"', src(f), f)

    def test_a_years_count_is_named(self):
        self.assertIn("· ${n} txns", src(PHONE + "spending.tsx"))


class OlderServerPaths(unittest.TestCase):
    """The phone in the stores also reads self-hosted servers older than
    the fields its screens print. Each stand-in it keeps for one says which
    field, on one line, so they can be retired together."""

    FIELDS = {
        "cashflow.tsx": ["`months_by_year`", "`kinds_shown`", "`shown`"],
        "networth.tsx": ["`rings.*.shown`"],
        "(tabs)/accounts.tsx": ["`status_kind`"],
        "(tabs)/bills.tsx": ["`period_frac`", "`amount_with_fees`",
                             "`due_label` / `due_text` / `due_tone` / `cycle_frac`",
                             "`attention_text`"],
        "budget.tsx": ["`monthly`", "`variable_budget_paced`", "`plan_legs`"],
        "merchants.tsx": ["`merged`", "`label` / `line`", "`total_label`"],
        "merchant.tsx": ["`rule.label`"],
        "reimburse.tsx": ["`oldest_label` / `waited_label`", "`amount_label`",
                          "`left_amount`", "`selection.line`"],
        "imports.tsx": ["`w2_people`"],
        "month.tsx": ["`pace_expected`", "`variance_text`",
                      "`projection.tail`", "`income_text`",
                      "`shown_used` / `shown_of`"],
        "year.tsx": ["`label`/`figure`", "`unknown_text`"],
        "alerts.tsx": ["`group`"],
        "(tabs)/index.tsx": ["`scenarios`", "`due_marks`", "`month_end_label`"],
    }

    def test_every_stand_in_names_the_field_it_stands_in_for(self):
        for f, fields in self.FIELDS.items():
            s = src(PHONE + f)
            for field in fields:
                self.assertIn(f"older-server path: stands in for {field}",
                              s, f"{f}: {field}")
        lib = src("mobile/src/lib/pure.ts") + src("mobile/src/lib/flowlayout.ts")
        for field in ["`cash_graph.windows`", "`heading`", "`so_far`",
                      "`months_by_year`", "`out.variable_scheduled`",
                      "`out.split_shown` / `shown`", "`figures_open_rows`",
                      "`drill_label`", "`summary`",
                      "`bank_link_line`"]:
            self.assertIn(f"older-server path: stands in for {field}", lib,
                          field)

    def test_a_present_field_is_printed_alone(self):
        # the stand-ins run only when the key is absent, never on a null
        # the server chose
        for f, needle in [("merchants.tsx", "p.merged !== undefined"),
                          ("(tabs)/index.tsx", "fc.scenarios !== undefined"),
                          ("reimburse.tsx", "oldest_label !== undefined"),
                          ("month.tsx", "d.pace_expected !== undefined"),
                          ("budget.tsx", "live.monthly !== undefined"),
                          ("(tabs)/bills.tsx", "b.period_frac === undefined"),
                          ("(tabs)/bills.tsx", "b.due_text === undefined"),
                          ("(tabs)/bills.tsx", "b.attention_text === undefined"),
                          ("budget.tsx", "d.plan_legs !== undefined"),
                          ("reimburse.tsx", "pairing.left_amount !== undefined"),
                          ("reimburse.tsx", "sel.data?.line !== undefined"),
                          ("month.tsx", "e.shown_used !== undefined"),
                          ("imports.tsx", "sent !== undefined")]:
            self.assertIn(needle, src(PHONE + f), f)


class Bills(unittest.TestCase):
    def test_the_cycle_is_read_from_the_cadence_value_never_the_label(self):
        # the web prints the server's fill; the phone's stand-in for an
        # older server reads the cadence value, never the label
        phone = src(PHONE + "(tabs)/bills.tsx")
        web = src(WEB + "Bills.tsx")
        self.assertNotIn("/^every (\\d+) (da|week|month|year)/", phone)
        self.assertNotIn("cadenceFromLabel", web)
        self.assertIn("billCycleDays(b.cadence_value)", phone)
        self.assertNotIn("billCycleDays(", web)
        self.assertIn("b.cycle_frac", web)

    def test_the_cycle_rule_is_one_text_in_both_clients(self):
        def body(s):
            i = s.index("export function billCycleDays")
            return s[i:s.index("\n}\n", i)]
        self.assertEqual(body(src("webapp/src/moneymath.ts")),
                         body(src("mobile/src/lib/pure.ts")))



class CashTimeline(unittest.TestCase):
    def test_a_card_paid_bill_greys_its_label_and_its_amount(self):
        web = src(WEB + "Today.tsx")
        phone = src(PHONE + "(tabs)/index.tsx")
        self.assertIn('r.bal_now === null || r.card || r.kind === "month_end"'
                      ' ? "mut"', web)
        self.assertIn("(r.bal_now === null || !!r.card)\n"
                      "                                  && { color: C.mut }",
                      phone)


if __name__ == "__main__":
    unittest.main()


class ClientsPrintTheServersWords(unittest.TestCase):
    """Words and figures both clients used to work out themselves, now
    printed as the server sends them."""

    def test_a_bill_rows_countdown_is_the_servers(self):
        web, phone = src(WEB + "Bills.tsx"), src(PHONE + "(tabs)/bills.tsx")
        for needle in ["const dueText", "daysUntil(", "SOON_DAYS"]:
            self.assertNotIn(needle, web, needle)
        self.assertIn("{b.due_text}", web)
        self.assertIn("b.due_label ??", web)
        self.assertIn("{due.text}", phone)
        self.assertIn("` · due ${due.label}`", phone)

    def test_the_attention_queue_prints_the_servers_sentence(self):
        web = src(WEB + "Bills.tsx")
        self.assertIn("const story = b.attention_text", web)
        self.assertNotIn("missed its${", web)

    def test_the_budget_line_prints_the_servers_legs(self):
        web = src(WEB + "Budget.tsx")
        self.assertIn("money(d.plan_legs.bills)", web)
        self.assertNotIn("d.plan_line ? d.plan_line", web)
        self.assertIn("m$(legs.to_spend)", src(PHONE + "budget.tsx"))

    def test_the_matcher_reads_the_pending_rows_room(self):
        web = src(WEB + "Reimburse.tsx")
        self.assertNotIn("Math.abs(anchor.amount) - (anchor.received", web)
        self.assertIn("?? anchor.left_amount", web)

    def test_the_income_tab_says_who_stopped_paying(self):
        self.assertIn("{w.dropped_text}", src(WEB + "CashFlow.tsx"))
        self.assertIn("{w.dropped_text}", src(PHONE + "cashflow.tsx"))

    def test_the_whose_picker_offers_the_servers_people(self):
        web, phone = src(WEB + "Import.tsx"), src(PHONE + "imports.tsx")
        self.assertNotIn("W2_PEOPLE", web)
        self.assertNotIn("W2_PEOPLE", phone)
        self.assertIn("list.data?.w2_people", web)
        self.assertIn("plan.w2_people", web)
        self.assertIn("w2PeopleOf(list.data?.w2_people)", phone)
        self.assertIn("w2PeopleOf(plan?.w2_people)", phone)


class TheForecastsTrackRecord(unittest.TestCase):
    """The forecast's track record (specs/forecast-grading.md) is the
    server's words on both clients, and its band replaces the heavier line
    only when the server sends one; an older server's phone keeps the
    heavier line, marked."""
    web = src(WEB + "Today.tsx")
    phone = src(PHONE + "(tabs)/index.tsx")

    def test_both_print_the_servers_sentences(self):
        for page, rec in ((self.web, "<ForecastRecord text={fc.score.text}"),
                          (self.phone, "<ForecastRecord client={client} "
                                       "text={fc.score.text}")):
            self.assertIn(rec, page)
            for field in ("fc.score.bias_text", "fc.score.note",
                          "scen.band_text"):
                self.assertIn(field, page, field)

    def test_the_band_is_drawn_as_sent_and_stands_in_for_the_heavier_line(
            self):
        for page in (self.web, self.phone):
            self.assertIn("band={scen.band ?? undefined}", page)
            self.assertIn(
                "second={scen.band ? undefined : scen.heavy ?? undefined}",
                page)
        self.assertIn("older-server path: stands in for `band`", self.phone)
        self.assertIn("older-server path: stands in for `score`", self.phone)

    def test_the_panel_and_the_drill_print_the_servers_words(self):
        for rel in ("webapp/src/components/ForecastRecord.tsx",
                    "mobile/src/components/forecast-record.tsx"):
            panel = src(rel)
            for field in ("coverage_text", "card_paying_text", "skips_text",
                          "empty_text", "x.label", "it.text", "m.error"):
                self.assertIn(field, panel, (rel, field))


class AStoppedPaycheckIsSaid(unittest.TestCase):
    """A paycheck the forecast leaves out because it stopped arriving is
    said in the server's sentence on the page, the phone and the email;
    the phone prints it outright, not under its folded notes."""

    def test_all_three_print_the_servers_sentence(self):
        self.assertIn("{fc.stopped_paychecks_note}", src(WEB + "Today.tsx"))
        phone = src(PHONE + "(tabs)/index.tsx")
        self.assertIn("{fc.stopped_paychecks_note}", phone)
        self.assertLess(phone.index("{fc.stopped_paychecks_note}"),
                        phone.index("disclosure(aboutOpen"))
        self.assertIn("{{ stopped_paychecks_note }}", src(
            "server/oikonome/web/templates/today_body.html"))
        self.assertIn("ctx['stopped_paychecks_note']",
                      src("server/oikonome/web/report.py"))


class CashAfterCardBalances(unittest.TestCase):
    """The list's second column is the server's after_cards under the
    server's heading; like every hide-m column it is off at phone width,
    on the web and on the phone alike."""

    def test_the_web_heads_it_in_the_servers_words(self):
        web = src(WEB + "Today.tsx")
        self.assertIn("fc?.after_cards_label", web)
        self.assertIn('<th className="num hide-m">{afterLabel}</th>', web)
        self.assertIn("money(r.after_cards)", web)

    def test_the_phone_follows_the_webs_phone_width(self):
        phone = src(PHONE + "(tabs)/index.tsx")
        self.assertIn("the rows' after_cards (the web's hide-m column,", phone)
        # the selector and the low words are the server's on both
        for client in (src(WEB + "Today.tsx"), phone):
            self.assertIn("x.low_words", client)
            self.assertIn("label: x.label", client)


class SeveralBanksInOneLink(unittest.TestCase):
    """A Plaid session that came back with several banks is worded by the
    server, and both clients print that sentence as it is; the line under
    the add-a-bank buttons says the same thing on both."""

    def test_both_print_the_servers_sentence(self):
        web = src("webapp/src/components/ConnectHub.tsx")
        self.assertIn('if (mode === "add" && s.summary) return { ok: true, '
                      'text: s.summary };', web)
        self.assertIn("if (s.summary) return { ok: false, text: s.summary };",
                      web)
        pure = src("mobile/src/lib/pure.ts")
        self.assertIn("if (r.summary !== undefined) return r.summary;", pure)
        self.assertIn("return { ok: true, text: addedLine(r) };",
                      src("mobile/src/lib/plaid.ts"))

    def test_the_line_under_the_buttons_is_the_servers(self):
        web = src("webapp/src/components/ConnectHub.tsx")
        self.assertIn("me.data?.features?.bank_link_line", web)
        self.assertIn("me?.features?.bank_link_line",
                      src("mobile/src/lib/pure.ts"))
        self.assertIn("bankLinkLine(me.data)",
                      src("mobile/src/components/pending-links.tsx"))
        for rel in (WEB + "Accounts.tsx", "webapp/src/components/ConnectHub.tsx",
                    PHONE + "connect-hub.tsx", PHONE + "welcome.tsx"):
            self.assertIn("<BankLinkLine />", src(rel), rel)
        for rel in ("webapp/src/components/ConnectHub.tsx",
                    "mobile/src/lib/pure.ts"):
            self.assertNotIn("SEVERAL_BANKS_LINE", src(rel), rel)

    def test_the_banks_so_far_show_on_the_pending_row_on_both(self):
        self.assertIn("if (s.progress) setPendingProgress(linkToken, "
                      "s.progress);", src("webapp/src/components/ConnectHub.tsx"))
        self.assertIn("if (p.progress && !p.cancelled) return p.progress;",
                      src("webapp/src/components/plaidlinks.ts"))
        self.assertIn("p.progress = s.progress", src("mobile/src/lib/plaid.ts"))
        self.assertIn("(!p.cancelled && p.progress) || pendingLinkLine(",
                      src("mobile/src/components/pending-links.tsx"))
        # a bank in is the answer, never a timeout
        for rel in ("webapp/src/components/ConnectHub.tsx",
                    "mobile/src/lib/plaid.ts"):
            self.assertIn("if (sofar) return sofar;\n", src(rel), rel)

    def test_both_say_they_keep_the_bank_window_open(self):
        # v=2: the server may answer a quiet several-bank session at 90 s;
        # a client that does not say so gets the longer clock
        self.assertIn("/status?v=2`", src("webapp/src/api/client.ts"))
        self.assertIn("/status?v=2`", src("mobile/src/lib/api.ts"))
        self.assertIn('"/status?resume=1&v=2"',
                      src("webapp/src/components/ConnectHub.tsx"))
        self.assertIn('"/status?resume=1&v=2"', src(PHONE + "connect-hub.tsx"))

    def test_the_first_sync_words_are_the_servers_too(self):
        from oikonome.jobs import plaid_links
        for rel in ("webapp/src/components/ConnectHub.tsx",
                    "mobile/src/lib/pure.ts"):
            self.assertIn(f'"{plaid_links.FIRST_SYNC_RUNNING}"', src(rel), rel)


class CardDueDates(unittest.TestCase):
    """Every view draws the server's due-date marks under the server's
    caption, on the web and the phone alike; the dated payments stand in
    only for an older server."""

    def test_both_draw_the_servers_due_marks(self):
        for client in (src(WEB + "Today.tsx"), src(PHONE + "(tabs)/index.tsx")):
            self.assertIn("dueMarks(scen.series, scen.due_marks ?? [])",
                          client)
            self.assertIn("scen.due_marks !== undefined", client)
            self.assertIn("older-server path: stands in for", client)
        for lib in (src(WEB + "Today.tsx"), src("mobile/src/lib/pure.ts")):
            self.assertIn("[{ i, label: m.label, note: m.words }]", lib)
