"""Tests for bubble_watch.py (LIMRA checks, spreads, weak borrowers, Oracle prepayments, stores). Run with: python3 -m unittest test_bubble_watch"""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

import bubble_watch as b

b.LAST_GOOD = tempfile.mkdtemp()  # saved copies from tests stay out of data/

TEXT = ("WINDSOR, Conn., June 11, 2026 — Total U.S. annuity sales were $107.4 billion in the first "
        "quarter of 2026, a 1% increase from the same period in 2025, according to results from "
        "LIMRA's U.S. Individual Annuity Sales Survey. Fixed indexed annuity (FIA) sales slipped 4% "
        "year over year to $26.6 billion in the first quarter of 2026.")
EVIDENCE = ("Total U.S. annuity sales were $107.4 billion in the first quarter of 2026, a 1% increase "
            "from the same period in 2025, according to results from LIMRA's U.S. Individual Annuity "
            "Sales Survey.")
REL = date(2026, 6, 11)


def model_line(quarter="2026-Q1", total="107.4", change="1% increase", evidence=EVIDENCE, prefix="QUARTER "):
    return f"{prefix}{quarter} | TOTAL ${total} billion | CHANGE {change} | EVIDENCE {evidence}"


class ParseChange(unittest.TestCase):
    def test_directions(self):
        cases = {"1% increase": 1, "2% below": -2, "down 12%": -12, "fell 13% year over year": -13,
                 "jumped 21%": 21, "level with": 0}
        for change, want in cases.items():
            self.assertEqual(b.parse_change(change, f"sales {change} the prior year"), want, change)

    def test_rejects_ambiguous_or_unquoted(self):
        self.assertIsNone(b.parse_change("up 26% from the first quarter and down 2%", "up 26% from the first quarter and down 2%"))
        self.assertIsNone(b.parse_change("5% increase", "sales rose"))  # not in the sentence
        self.assertIsNone(b.parse_change("NONE", "anything"))
        self.assertIsNone(b.parse_change("14%", "14%"))  # no direction


class VerifyQuarter(unittest.TestCase):
    def test_accepts_exact_quote(self):
        q = b.verify_quarter(model_line(), TEXT, REL)
        self.assertEqual((q["quarter"], q["total"], q["yoy"]), ("2026-Q1", 107.4, 1.0))

    def test_accepts_missing_prefix(self):
        self.assertIsNotNone(b.verify_quarter(model_line(prefix=""), TEXT, REL))

    def test_rejects_wrong_total(self):
        self.assertIsNone(b.verify_quarter(model_line(total="104.6"), TEXT, REL))

    def test_rejects_paraphrase(self):
        para = "Total annuity sales were $107.4 billion in the first quarter of 2026, up 1%."
        self.assertIsNone(b.verify_quarter(model_line(evidence=para), TEXT, REL))

    def test_rejects_wrong_quarter(self):
        self.assertIsNone(b.verify_quarter(model_line(quarter="2026-Q2"), TEXT, REL))

    def test_rejects_product_line(self):
        fia = "Fixed indexed annuity (FIA) sales slipped 4% year over year to $26.6 billion in the first quarter of 2026."
        self.assertIsNone(b.verify_quarter(model_line(total="26.6", change="slipped 4%", evidence=fia), TEXT, REL))

    def test_rejects_implausible_year(self):
        self.assertIsNone(b.verify_quarter(model_line(quarter="2024-Q1"), TEXT, REL))


class Quarters(unittest.TestCase):
    def test_next_and_end(self):
        self.assertEqual(b.next_quarter("2025-Q4"), "2026-Q1")
        self.assertEqual(b.next_quarter("2026-Q1"), "2026-Q2")
        self.assertEqual(b.quarter_end("2025-Q4"), date(2025, 12, 31))
        self.assertEqual(b.quarter_end("2026-Q1"), date(2026, 3, 31))


class LimraChecks(unittest.TestCase):
    Q2 = ("Fixed-rate deferred (FRD) annuity sales were $44.7 billion in the second quarter, up 26% from the "
          "first quarter and down 2% from the second quarter of 2025.")

    def test_quarter_on_quarter_is_not_year_over_year(self):
        self.assertIsNone(b.parse_change("up 26%", self.Q2, 2026, 2))
        self.assertEqual(b.parse_change("down 2%", self.Q2, 2026, 2), -2)
        ev = "Total U.S. annuity sales rose 2% year over year to $121.2 billion in the second quarter of 2026."
        self.assertEqual(b.parse_change("rose 2%", ev, 2026, 2), 2)

    def test_multi_sentence_evidence_is_rejected(self):
        two = EVIDENCE + " Fixed indexed annuity (FIA) sales slipped 4% year over year to $26.6 billion in the first quarter of 2026."
        self.assertIsNone(b.verify_quarter(model_line(evidence=two), TEXT + " " + two, REL))

    def test_half_year_total_is_rejected(self):
        ev = "Total annuity sales were $228.7 billion in the first half of 2026, 1% higher than the second quarter of 2025."
        self.assertIsNone(b.verify_quarter(model_line(quarter="2026-Q2", total="228.7", change="1% higher", evidence=ev), ev, REL))

    def test_expected_quarter(self):
        self.assertEqual(b.expected_limra_quarter(date(2026, 10, 20)), "2026-Q2")  # Q3 results not due yet
        self.assertEqual(b.expected_limra_quarter(date(2026, 11, 20)), "2026-Q3")
        self.assertEqual(b.expected_limra_quarter(date(2027, 1, 15)), "2026-Q3")
        self.assertEqual(b.expected_limra_quarter(date(2027, 2, 15)), "2026-Q4")

    def test_change_formatting(self):
        self.assertEqual(b.fmt_change(-0.4), "0%")
        self.assertEqual(b.fmt_change(1.7), "+2%")
        self.assertEqual(b.fmt_change(-2.0), "-2%")


class LeadSentence(unittest.TestCase):
    def test_total_in_the_opening_lines_counts_without_the_words_annuity_sales(self):
        text = ("WINDSOR, Conn., March 23, 2026— Total U.S. annuity sales increased 7% to $464.1 billion in 2025. "
                "Fourth quarter sales jumped 14% to $117.2 billion, representing the ninth consecutive quarter. "
                + "x" * 900 + " Fixed indexed annuity sales were $70.9 billion in the fourth quarter of 2025.")
        ev = "Fourth quarter sales jumped 14% to $117.2 billion, representing the ninth consecutive quarter."
        q = b.verify_quarter(model_line(quarter="2025-Q4", total="117.2", change="jumped 14%", evidence=ev), text, date(2026, 3, 23))
        self.assertEqual((q["quarter"], q["total"], q["yoy"]), ("2025-Q4", 117.2, None))  # no year-ago words: computed later
        late = "Fixed indexed annuity sales were $70.9 billion in the fourth quarter of 2025."
        self.assertIsNone(b.verify_quarter(model_line(quarter="2025-Q4", total="70.9", change="NONE", evidence=late), text, date(2026, 3, 23)))


class CdsHeadlines(unittest.TestCase):
    def test_bounds_are_not_levels(self):
        self.assertTrue(b.is_bound("CoreWeave CDS spreads exceeded 1,000 bps - CNBC", "1,000"))
        self.assertTrue(b.is_bound("CoreWeave CDS Spreads Top 1,000bp", "1,000"))
        self.assertFalse(b.is_bound("CNBC reports Oracle CDS at 295 basis points", "295"))


class Spreads(unittest.TestCase):
    def test_threshold_is_exact(self):
        csv_text = "observation_date,BAMLC0A4CBBB\n" + "\n".join(
            f"{date(2025, 1, 1) + timedelta(days=i)},{'1.15' if i >= 500 else '0.85'}" for i in range(520))
        with mock.patch.object(b, "_download", return_value=csv_text.encode()):
            rows = b.fred_series("BAMLC0A4CBBB")
        self.assertEqual(rows[-1][1] - min(v for _, v in rows[-366:]), 30)
        first, episodes = b.spread_episodes(rows, 30)
        self.assertEqual(len(episodes), 1)

    def test_one_series_down_is_checked_by_hand(self):
        csv_text = "observation_date,BAMLC0A4CBBB\n" + "\n".join(
            f"{date(2025, 1, 1) + timedelta(days=i)},0.85" for i in range(520))
        def download(url, ua, tries):
            if url.endswith("BAMLC0A4CBBB"):
                return csv_text.encode()
            return b"<!doctype html><title>Site Maintenance | St. Louis Fed</title>"
        with mock.patch.object(b, "_download", side_effect=download):
            page = b.block_spreads()
        self.assertIn("checking by hand today", page)
        self.assertIn("Site Maintenance", page)
        self.assertIn("not available today", page)


class WeakBorrowers(unittest.TestCase):
    def test_falling_together_while_market_holds_up_warns(self):
        def closes(ticker, span="1y"):
            start = 100.0
            end = 99.0 if ticker in ("QQQ", "ORCL") else 70.0
            return [(str(date(2026, 1, 1) + timedelta(days=i)), start if i < 60 else end) for i in range(80)]
        with mock.patch.object(b, "yahoo_closes", side_effect=closes):
            self.assertIn("a warning today", b.block_weak_borrowers())
        def calm(ticker, span="1y"):
            return [(str(date(2026, 1, 1) + timedelta(days=i)), 100.0) for i in range(80)]
        with mock.patch.object(b, "yahoo_closes", side_effect=calm):
            self.assertIn("no warning today", b.block_weak_borrowers())


class OraclePrepayments(unittest.TestCase):
    def test_quarters_from_year_to_date_figures(self):
        store = {"a": {"period": "2026-08-31", "form": "10-Q", "facts": [["2026-06-01", "2026-08-31", 11363e6]]},
                 "b": {"period": "2026-05-31", "form": "10-K", "facts": [["2025-06-01", "2026-05-31", 4592e6]]},
                 "c": {"period": "2026-02-28", "form": "10-Q", "facts": []}}
        empty = {"filings": {"recent": {"form": [], "accessionNumber": [], "reportDate": []}}}
        with mock.patch.object(b, "load_json_store", return_value=store), \
                mock.patch.object(b, "save_json_store"), mock.patch.object(b, "fetch_json", return_value=empty):
            q = b.oracle_prepayments()
        self.assertEqual(q["2026-08-31"], (11363e6, "2026-06-01"))
        self.assertEqual(q["2026-05-31"], (4592e6, "2025-06-01"))  # nothing earlier: covers the whole year

    def test_remainder_starts_after_the_latest_earlier_figure(self):
        store = {"q1": {"period": "2025-08-31", "form": "10-Q", "facts": [["2025-06-01", "2025-08-31", 0.0]]},
                 "q2": {"period": "2026-11-30", "form": "10-Q", "facts": [["2025-06-01", "2025-11-30", 1e9]]},
                 "k": {"period": "2026-05-31", "form": "10-K", "facts": [["2025-06-01", "2026-05-31", 4592e6]]}}
        empty = {"filings": {"recent": {"form": [], "accessionNumber": [], "reportDate": []}}}
        with mock.patch.object(b, "load_json_store", return_value=store), \
                mock.patch.object(b, "save_json_store"), mock.patch.object(b, "fetch_json", return_value=empty):
            q = b.oracle_prepayments()
        self.assertEqual(q["2025-11-30"], (1e9, "2025-09-01"))
        self.assertEqual(q["2026-05-31"], (3592e6, "2025-12-01"))  # the first half is taken out


class Quarterly(unittest.TestCase):
    def test_consecutive(self):
        self.assertTrue(b.consecutive(["2025-09-30", "2025-12-31", "2026-03-31"]))
        self.assertFalse(b.consecutive(["2025-09-30", "2026-03-31", "2026-06-30"]))

    def test_nvidia_with_a_missing_quarter_is_checked_by_hand(self):
        facts = [{"start": s, "end": e, "val": v, "filed": "2026-08-01"} for s, e, v in
                 [("2025-07-01", "2025-09-30", 50e9), ("2026-01-01", "2026-03-31", 40e9)]]
        ar = [{"end": "2026-03-31", "val": 20e9, "filed": "2026-08-01"}]
        with mock.patch.object(b, "sec_concept",
                               side_effect=lambda cik, c: ar if c == "AccountsReceivableNetCurrent" else facts):
            page = b.block_nvidia()
        self.assertIn("checking by hand today", page)
        self.assertNotIn("a warning today", page)


def quarter_ends(n, last="2026-06-30"):
    """n consecutive calendar quarter ends up to last."""
    y, m = int(last[:4]), int(last[5:7])
    out = []
    for _ in range(n):
        out.append(date(y, m, 30 if m in (6, 9) else 31).isoformat())
        y, m = (y, m - 3) if m > 3 else (y - 1, 12)
    return out[::-1]


class Receivables(unittest.TestCase):
    def series(self, latest_ar, n=14):
        ends = quarter_ends(n)
        rev = {e: 90e9 for e in ends}
        ar = {e: 45e9 for e in ends}  # about 45 days of sales
        ar[ends[-1]] = latest_ar
        return rev, ar

    def test_three_year_high_warns(self):
        page = b.receivables_line(*self.series(60e9))
        self.assertIn("a warning today", page)
        self.assertIn("60.7 days of sales on Jun 30, 2026", page)

    def test_below_the_high_does_not(self):
        rev, ar = self.series(44e9)
        ar[quarter_ends(14)[5]] = 55e9  # an earlier high inside the three years
        page = b.receivables_line(rev, ar)
        self.assertIn("no warning today", page)
        self.assertIn("previous three-year high was 55.6", page)

    def test_missing_receivables_is_checked_by_hand(self):
        rev, ar = self.series(60e9)
        del ar[quarter_ends(14)[-1]]
        self.assertIn("checking by hand today", b.receivables_line(rev, ar))
        self.assertIn("checking by hand today", b.receivables_line(rev, {}, "sec.gov down"))

    def test_too_little_history_is_checked_by_hand(self):
        self.assertIn("checking by hand today", b.receivables_line(*self.series(60e9, n=6)))


class HeadlineCap(unittest.TestCase):
    def test_small_feeds_keep_everything(self):
        q = b.feed_quotas({"A": 18, "B": 62, "C": 670, "D": 321, "E": 2, "F": 16}, 499)
        self.assertEqual((q["A"], q["B"], q["E"], q["F"]), (18, 62, 2, 16))
        self.assertEqual(sum(q.values()), 499)
        self.assertLessEqual(abs(q["C"] - q["D"]), 1)

    def test_under_the_cap_nothing_is_dropped(self):
        self.assertEqual(b.feed_quotas({"A": 1, "B": 8}, 10), {"A": 1, "B": 8})

    def test_capped_feed_with_nothing_flagged_is_not_clear(self):
        story = {"headlines": [{"date": "2026-10-19", "link": "#", "title": "t", "when": None}],
                 "verdict": "ok", "reason": "", "new": False}
        feeds = {"B": {"stories": [story], "error": None, "capped": True, "available": 62}}
        with mock.patch.object(b, "headlines", return_value=(feeds, "")):
            _, crossed, detail = b.feed_block("B")
        self.assertIsNone(crossed)
        self.assertIn("1 of 62", detail)
        feeds["B"]["capped"] = False
        with mock.patch.object(b, "headlines", return_value=(feeds, "")):
            self.assertIs(b.feed_block("B")[1], False)

    def test_only_flagged_stories_with_a_link_count(self):
        story = {"headlines": [{"date": "2026-10-19", "link": "#", "title": "t", "when": None}],
                 "verdict": "alert", "reason": "r", "new": False}
        feeds = {"C": {"stories": [story], "error": None, "capped": False, "available": 1}}
        with mock.patch.object(b, "headlines", return_value=(feeds, "")):
            self.assertIsNone(b.feed_block("C")[1])
        story["headlines"][0]["link"] = "https://example.com/a"
        with mock.patch.object(b, "headlines", return_value=(feeds, "")):
            self.assertIs(b.feed_block("C")[1], True)


class CarriedOver(unittest.TestCase):
    def rec(self, days_ago, feed="A"):
        return {"feed": feed, "title": "T - Outlet", "link": "https://example.com/t",
                "when": (b.NOW - timedelta(days=days_ago)).isoformat()}

    def test_recent_headline_missing_from_today_is_kept(self):
        known = {"recent": self.rec(5), "old": self.rec(40), "other feed": self.rec(5, "B"),
                 "found today": self.rec(5), "no date": {"feed": "A", "first_seen": "2026-10-01"}}
        taken = {"found today"}
        kept = b.carried_over(known, "A", taken)
        self.assertEqual([h["key"] for h in kept], ["recent"])
        self.assertTrue(kept[0]["carried"])
        self.assertIn("recent", taken)

    def test_feed_says_how_many_were_kept(self):
        story = {"headlines": [{"date": "2026-10-19", "link": "https://example.com/a", "title": "t",
                                "when": None}], "verdict": "alert", "reason": "r", "new": False}
        feeds = {"A": {"stories": [story], "error": None, "capped": False, "available": 1, "carried": 1}}
        with mock.patch.object(b, "headlines", return_value=(feeds, "")):
            html, crossed, _ = b.feed_block("A")
        self.assertIn("1 of these was missing from today's search", html)
        self.assertIs(crossed, True)


class Stores(unittest.TestCase):
    def test_cached_runs_never_write(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(b, "CACHED", True):
            path = os.path.join(tmp, "x.json")
            b.save_json_store(path, {"a": 1})
            self.assertFalse(os.path.exists(path))

    def test_corrupt_store_is_set_aside(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(b, "CACHED", False):
            path = os.path.join(tmp, "x.json")
            with open(path, "w") as f:
                f.write("{not json")
            self.assertEqual(b.load_json_store(path), {})
            self.assertTrue(any(n.startswith("x.json.corrupt-") for n in os.listdir(tmp)))
            self.assertTrue(b.WARNINGS)


def quarters(n=8, start=date(2024, 4, 1), val=lambda i: 10e9 + i * 1e9):
    """Synthetic three-month XBRL flow facts for n consecutive calendar quarters."""
    out, s = [], start
    for i in range(n):
        after = date(s.year + (s.month + 2) // 12, (s.month + 2) % 12 + 1, 1)  # first day after the quarter
        e = after - timedelta(days=1)
        out.append({"start": s.isoformat(), "end": e.isoformat(), "val": val(i), "filed": "2026-08-01"})
        s = e + timedelta(days=1)
    return out


class OneSourceDown(unittest.TestCase):
    """One company, ticker or series that can't be read leaves the others, and no verdict says No warning."""

    def test_hyperscalers(self):
        def concept(cik, name):
            if cik == 1652044:
                raise RuntimeError("SEC 503")
            return quarters(val=(lambda i: 30e9 + i * 1e9) if name.startswith("NetCash") else (lambda i: 10e9 + i * 1e9))
        with mock.patch.object(b, "sec_concept", side_effect=concept), \
                mock.patch.object(b, "sec_concept_or_empty", return_value=[]), \
                mock.patch.object(b, "oracle_prepayments", return_value={}), mock.patch.object(b, "TODAY", date(2026, 5, 1)):
            page = b.block_hyperscalers()
        self.assertIn("not available today (SEC 503)", page)
        self.assertIn("<h5>MSFT</h5>", page)
        self.assertEqual(page.count("checking by hand today"), 2)
        self.assertNotIn("no warning today", page)

    def test_weak_borrowers(self):
        def closes(ticker, span="1y"):
            if ticker == "IREN":
                raise RuntimeError("Yahoo timeout")
            return [(str(date(2026, 1, 1) + timedelta(days=i)), 100.0) for i in range(80)]
        with mock.patch.object(b, "yahoo_closes", side_effect=closes):
            page = b.block_weak_borrowers()
        self.assertIn("checking by hand today", page)
        self.assertIn("Yahoo timeout", page)
        self.assertIn("<td>CRWV</td>", page)

    def test_gpu_prices(self):
        weeks = [((b.GPU_START + timedelta(weeks=i)).isoformat(), 2.0, 10) for i in range(30)]
        def gpu(slug, billing="ON_DEMAND", months=""):
            if slug == "nvidia-b200":
                raise RuntimeError("no on-demand rows for nvidia-b200")
            return weeks if billing == "ON_DEMAND" else []
        with mock.patch.object(b, "gpu_weekly", side_effect=gpu):
            page = b.block_gpu_prices()
        self.assertIn("checking by hand today", page)
        self.assertIn("H100", page)

    def test_every_source_down_fails_the_signal(self):
        with mock.patch.object(b, "yahoo_closes", side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError):
                b.block_weak_borrowers()

    def test_web_page_instead_of_json(self):
        with mock.patch.object(b, "_download", return_value=b"<html><title>Site Maintenance</title></html>"):
            with self.assertRaisesRegex(RuntimeError, "data.sec.gov sent a web page instead of data .Site Maintenance."):
                b.sec_concept(1, "X")


class LastGood(unittest.TestCase):
    URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2"
    GOOD = b"observation_date,BAMLH0A0HYM2\n2026-10-19,2.80\n2026-10-20,2.85\n"

    def test_saved_copy_is_used_and_said_so(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(b, "LAST_GOOD", tmp), \
                mock.patch.object(b, "FALLBACKS", []), mock.patch.object(b, "CACHED", False):
            with mock.patch.object(b, "_download", return_value=self.GOOD):
                b.fred_series("BAMLH0A0HYM2")
            with mock.patch.object(b, "_download", return_value=b"<title>Site Maintenance</title>"):
                rows = b.fred_series("BAMLH0A0HYM2")
            self.assertEqual(rows[-1], ("2026-10-20", 285.0))
            self.assertEqual(len(b.FALLBACKS), 1)
            self.assertIn("fred.stlouisfed.org (BAMLH0A0HYM2)", b.FALLBACKS[0])
            self.assertIn("uses the copy saved", b.FALLBACKS[0])

    def test_bad_response_never_replaces_the_saved_copy(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(b, "LAST_GOOD", tmp), \
                mock.patch.object(b, "FALLBACKS", []), mock.patch.object(b, "CACHED", False):
            with mock.patch.object(b, "_download", return_value=b"<title>Site Maintenance</title>"):
                with self.assertRaises(RuntimeError):  # nothing saved yet
                    b.fred_series("BAMLH0A0HYM2")
            self.assertEqual(os.listdir(tmp), [])

    def test_not_found_is_an_answer(self):
        err = b.urllib.error.HTTPError(self.URL, 404, "Not Found", {}, None)
        with mock.patch.object(b, "_download", side_effect=err), mock.patch.object(b, "CACHED", False):
            self.assertEqual(b.sec_concept_or_empty(1, "X"), [])

    def test_stale_ticker_is_not_averaged(self):
        def closes(ticker, span="1y"):
            n = 79 if ticker == "IREN" else 80  # IREN from an older saved copy
            return [(str(date(2026, 1, 1) + timedelta(days=i)), 100.0 if i < 60 else 70.0) for i in range(n)]
        page = None
        with mock.patch.object(b, "yahoo_closes", side_effect=closes):
            page = b.block_weak_borrowers()
        self.assertIn("close of 2026-03-20", page)
        self.assertIn("checking by hand today", page)


def gpu_weeks(prices, sellers=10):
    """Weekly GPU rows from GPU_START on, one per price."""
    return [((b.GPU_START + timedelta(weeks=i)).isoformat(), p, sellers) for i, p in enumerate(prices)]


class SpotPrices(unittest.TestCase):
    def page(self, spot, sellers=10):
        def gpu(slug, billing="ON_DEMAND", months=""):
            return {"ON_DEMAND": gpu_weeks([4.0] * 30), "SPOT": gpu_weeks(spot, sellers)}.get(billing, [])
        with mock.patch.object(b, "gpu_weekly", side_effect=gpu):
            return b.block_gpu_prices()

    def test_change_compares_four_week_averages(self):
        weeks = gpu_weeks([2.0] * 17 + [1.0] * 4)
        last = date.fromisoformat(weeks[-1][0])
        self.assertEqual(b.gpu_change(weeks, last, 13), -50)
        self.assertIsNone(b.gpu_change(weeks[1:], last, 17))  # the earlier stretch has a week missing
        self.assertIsNone(b.gpu_change(weeks, last, 13, min_sellers=11))

    def test_falling_spot_price_warns_while_on_demand_is_flat(self):
        page = self.page([3.0] * 17 + [2.0] * 13)
        self.assertEqual(page.count("no warning today"), 1)  # on-demand
        self.assertEqual(page.count("a warning today"), 1)     # spot
        self.assertIn("The spot price fell 20% or more over 13 weeks for H100, H200 and B200", page)

    def test_steady_spot_price_does_not(self):
        self.assertEqual(self.page([3.0] * 30).count("no warning today"), 2)

    def test_too_few_sellers_is_checked_by_hand(self):
        page = self.page([3.0] * 17 + [2.0] * 13, sellers=2)
        self.assertIn("too few spot sellers to compare H100, H200 and B200", page)
        self.assertNotIn("a warning today", page)


def fred_quarters(values, first=1990):
    """[(quarter start, value)] for consecutive quarters from the first quarter of `first`."""
    return [(date(first + i // 4, i % 4 * 3 + 1, 1).isoformat(), v) for i, v in enumerate(values)]


class Buildout(unittest.TestCase):
    def page(self, tech):
        n = len(tech)
        series = {"GDP": fred_quarters([1000.0] * n), "A679RC1Q027SBEA": fred_quarters(tech),
                  "B935RC1Q027SBEA": fred_quarters([10.0] * n)}
        today = date(1990 + n // 4, 1, 1)
        with mock.patch.object(b, "fred_series", side_effect=lambda sid, scale=100: series[sid]), \
                mock.patch.object(b, "TODAY", today):
            return b.block_buildout()

    def test_episodes(self):
        pts = fred_quarters([4.0] * 10 + [3.9, 3.7, 3.6, 4.2, 4.2])
        self.assertEqual([(a, z, round(x, 2)) for a, z, x in b.drop_episodes(pts, 0.25)],
                         [("1992-10-01", "1993-01-01", 0.4)])

    def test_fall_from_the_two_year_high_warns(self):
        page = self.page([40.0] * 20 + [45.0, 50.0, 47.0])  # 5.0% of GDP, then 4.7%
        self.assertIn("a warning today", page)
        self.assertIn("0.30 points below its two-year high of 5.00% in Q2 1995", page)
        self.assertIn("That hasn't happened since 1992.", page)

    def test_at_the_high_does_not(self):
        page = self.page([40.0] * 20 + [45.0, 50.0])
        self.assertIn("no warning today", page)
        self.assertIn("its highest in two years", page)

    def test_narrow_series_down_leaves_the_line(self):
        def fred(sid, scale=100):
            if sid.startswith("B935"):
                raise RuntimeError("FRED down")
            return fred_quarters([1000.0] * 22) if sid == "GDP" else fred_quarters([40.0] * 20 + [45.0, 50.0])
        with mock.patch.object(b, "fred_series", side_effect=fred), mock.patch.object(b, "TODAY", date(1995, 7, 1)):
            page = b.block_buildout()
        self.assertIn("no warning today", page)
        self.assertIn("not available today (FRED down)", page)


def xlsx(sheets):
    """A minimal .xlsx with inline strings: {sheet name: [[cell, ...], ...]}."""
    import io, zipfile
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} xmlns:r="{rel}"><sheets>' + "".join(
            f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(sheets, 1)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(
                       f'<Relationship Type="{rel}/worksheet" Target="/xl/worksheets/sheet{i}.xml" Id="rId{i}"/>'
                       for i in range(1, len(sheets) + 1)) + "</Relationships>")
        for i, rows in enumerate(sheets.values(), 1):
            cells = "".join(
                f'<row r="{r}">' + "".join(
                    f'<c r="{chr(64 + c)}{r}" t="inlineStr"><is><t>{v}</t></is></c>' for c, v in enumerate(row, 1) if v != "")
                + "</row>" for r, row in enumerate(rows, 1))
            z.writestr(f"xl/worksheets/sheet{i}.xml", f"<worksheet {ns}><sheetData>{cells}</sheetData></worksheet>")
    return buf.getvalue()


class Adoption(unittest.TestCase):
    USE = "In the last two weeks, did this business use Artificial Intelligence (AI) in any of its business functions? (Examples)"
    NEXT = "During the next six months, do you think this business will be using Artificial Intelligence (AI)?"
    JOBS = "In the last six months, how did this business’s use of Artificial Intelligence affect the number of employees (total headcount) at this business?"

    def book(self, shares):
        surveys = [f"2026{i + 1:02d}" for i in range(len(shares))]
        head = ["Question ID", "Question", "Answer ID", "Answer"] + surveys[::-1]  # newest first, as published
        rows = [head,
                ["7", self.USE, "1", "Yes"] + [f"{v}%" for v in shares[::-1]],
                ["7", self.USE, "2", "No"] + [f"{100 - v}%" for v in shares[::-1]],
                ["8", "In the last two weeks, did this business’s employees use Generative Artificial Intelligence?", "1", "Yes"]
                + ["99.0%"] * len(shares),
                ["28", self.NEXT, "1", "Yes"] + [f"{v + 3}%" for v in shares[::-1]],
                ["9", self.JOBS, "1", "Increased"] + ["6.6%"] + ["."] * (len(shares) - 1),
                ["9", self.JOBS, "2", "Decreased"] + ["2.5%"] + ["."] * (len(shares) - 1)]
        first = (date(2026, 1, 4) - date(1899, 12, 30)).days  # Excel's day number
        dates = [["Sample Year", "Cycle", "Panel", "Smpdt", "Collection Start", "Col End"]] + [
            ["", "1", "1", s, str(first + 14 * i - 13), str(first + 14 * i)] for i, s in enumerate(surveys)]
        return xlsx({"Response Estimates": rows, "Collection and Reference Dates": dates})

    def page(self, shares, today=None):
        last = date(2026, 1, 4) + timedelta(days=14 * (len(shares) - 1))
        with mock.patch.object(b, "_download", return_value=self.book(shares)), \
                mock.patch.object(b, "TODAY", today or last + timedelta(days=14)):
            return b.block_adoption()

    def test_reads_the_right_question_and_dates(self):
        with mock.patch.object(b, "_download", return_value=self.book([20.0, 21.0, 22.5])):
            answers, ends = b.btos()
        question, use = b.btos_series(answers, ends, b.BTOS_USE)
        self.assertEqual(question, self.USE)
        self.assertEqual(use, [("2026-01-04", 20.0), ("2026-01-18", 21.0), ("2026-02-01", 22.5)])
        self.assertEqual(b.btos_series(answers, ends, b.BTOS_NEXT)[1][-1], ("2026-02-01", 25.5))

    def test_rising_use_is_no_warning(self):
        page = self.page([18.0 + i * 0.5 for i in range(12)])
        self.assertIn("no warning today", page)
        self.assertIn("2.5% of businesses said AI had cut their headcount", page)

    def test_flat_use_warns(self):
        self.assertIn("a warning today", self.page([18.0, 19.0, 20.0] + [21.0] * 9))

    def test_old_or_short_series_is_checked_by_hand(self):
        self.assertIn("enough surveys yet", self.page([18.0, 19.0, 20.0]))
        page = self.page([18.0 + i * 0.5 for i in range(12)], today=date(2027, 1, 1))
        self.assertIn("which is unusually old", page)

    def test_web_page_instead_of_a_spreadsheet(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(b, "LAST_GOOD", tmp), \
                mock.patch.object(b, "_download", return_value=b"<html><title>Access Denied</title></html>"):
            with self.assertRaisesRegex(RuntimeError, "sent a web page instead of data .Access Denied."):
                b.btos()  # nothing saved to fall back on


class Payback(unittest.TestCase):
    """Capex of $10B a quarter needs 40 x 2/7 = $11.4B of new revenue a year."""

    def page(self, revenue_step, missing=None):
        def concept(cik, name):
            if cik == missing:
                raise RuntimeError("SEC 503")
            return quarters(val=lambda i: 10e9)
        def other(cik, name):
            if name == b.REVENUE_CONCEPTS[0]:
                return quarters(val=lambda i: 50e9 + i * revenue_step)
            return quarters(val=lambda i: 2e9 + i * 1e9) if name == "Depreciation" else []
        with mock.patch.object(b, "sec_concept", side_effect=concept), \
                mock.patch.object(b, "sec_concept_or_empty", side_effect=other), \
                mock.patch.object(b, "TODAY", date(2026, 5, 1)):
            return b.block_payback()

    def test_enough_new_revenue_is_no_warning(self):
        page = self.page(1e9)  # each quarter $1B above the last: $16B more over four quarters
        self.assertIn("no warning today", page)
        self.assertIn("<td>11.4</td><td>16.0</td><td>1.40</td>", page)
        self.assertIn("All 5 together added $80B against $57B needed", page)

    def test_too_little_new_revenue_warns(self):
        page = self.page(0.5e9)  # $8B more
        self.assertIn("a warning today", page)
        self.assertIn('<span class="bad">0.70</span>', page)
        self.assertIn("MSFT, GOOGL, AMZN, META and ORCL added less revenue than its spending calls for", page)

    def test_one_company_down_is_checked_by_hand(self):
        page = self.page(1e9, missing=1652044)
        self.assertIn("checking by hand today", page)
        self.assertIn("not available today (SEC 503)", page)
        self.assertIn("All 4", page)

    def test_newest_tag_wins(self):
        old, new = quarters(n=4), quarters(n=8)
        with mock.patch.object(b, "sec_concept_or_empty", side_effect=lambda cik, c: new if c == "Revenues" else old):
            self.assertEqual(len(b.newest_flows(1, b.REVENUE_CONCEPTS)), 8)


class NvidiaFinancing(unittest.TestCase):
    def page(self, gains):
        ends = quarter_ends(8)
        stakes = [{"end": e, "val": (i + 1) * 5e9, "filed": "2026-08-01"} for i, e in enumerate(ends)]
        flows = quarters(n=len(gains), start=date(2024, 7, 1), val=lambda i: gains[i])
        def other(cik, name):
            if name == "GainLossOnInvestments":
                return flows
            return stakes if name.startswith("EquitySecuritiesWithout") else []
        with mock.patch.object(b, "sec_concept_or_empty", side_effect=other), \
                mock.patch.object(b, "sec_concept", return_value=quarters(val=lambda i: 50e9)), \
                mock.patch.object(b, "TODAY", date(2026, 8, 1)):
            return b.block_nvidia_financing()

    def test_gain_is_no_warning(self):
        page = self.page([1e9] * 8)
        self.assertIn("no warning today", page)
        self.assertIn("$40.0B (2026-06-30)</td><td>$20.0B (2025-06-30)</td><td>×2.0", page)
        self.assertIn("not reported", page)  # the two figures with no facts

    def test_loss_warns(self):
        page = self.page([1e9] * 7 + [-2e9])
        self.assertIn("a warning today", page)
        self.assertIn("a loss of $2.0B on its investments in the quarter to Jun 30, 2026, after a gain of $1.0B the quarter before", page)

    def test_untagged_gains_are_checked_by_hand(self):
        self.assertIn("doesn't tag its investment gains and losses", self.page([]))


class Landlords(unittest.TestCase):
    def page(self, six, landlords):
        def closes(ticker, span="1y"):
            end = landlords if ticker in dict(b.LANDLORDS) else 99.0 if ticker in ("QQQ", "ORCL") else six
            return [(str(date(2026, 1, 1) + timedelta(days=i)), 100.0 if i < 60 else end) for i in range(80)]
        with mock.patch.object(b, "yahoo_closes", side_effect=closes):
            return b.block_weak_borrowers()

    def test_chip_owners_falling_alone_warns_on_both_lines(self):
        page = self.page(70.0, 101.0)
        self.assertEqual(page.count("a warning today"), 2)
        self.assertIn("the six fell 30.0% on average and the landlords rose 1.0%", page)

    def test_everything_falling_together_warns_only_against_the_market(self):
        page = self.page(70.0, 70.0)
        self.assertEqual(page.count("a warning today"), 1)
        self.assertEqual(page.count("no warning today"), 1)


class Glance(unittest.TestCase):
    def test_a_section_is_listed_under_its_worst_line(self):
        with mock.patch.object(b, "STATUS", [("Spreads", "spreads", False), ("Capex", "capex", False),
                                              ("Capex", "capex", True), ("Annuities", "annuities", None)]):
            page = b.glance()
        self.assertIn('There\'s a warning today</span> in these sections.</p><ul><li><a href="#capex">Capex</a></li></ul>', page)
        self.assertIn('checking by hand today.</p><ul><li><a href="#annuities">Annuities</a></li></ul>', page)
        self.assertIn('no warning today in these.</p><ul><li><a href="#spreads">Spreads</a></li></ul>', page)

    def test_lines_are_recorded_under_the_section_being_built(self):
        with mock.patch.object(b, "STATUS", []), mock.patch.object(b, "SECTION", [("GPU rental prices", "gpu-rental-prices")]):
            b.line("rule", True)
            self.assertEqual(b.STATUS, [("GPU rental prices", "gpu-rental-prices", True)])


class NewFeed(unittest.TestCase):
    def test_reply_lines_for_every_feed_are_read(self):
        reply = {"choices": [{"finish_reason": "stop", "message": {"content":
                 "G1 NEW ALERT | Neocloud files for Chapter 11 | GPU cloud bankruptcy\nG2 SAME G1\nA1 NEW OK | Lease signed"}}],
                 "usage": {"total_tokens": 10}}
        with mock.patch.object(b, "check_model"), mock.patch.object(b, "deepseek", return_value=reply):
            new, same, tokens = b.classify([])
        self.assertEqual(new["G1"], ("ALERT", "Neocloud files for Chapter 11", "GPU cloud bankruptcy"))
        self.assertEqual(same, {"G2": "G1"})
        self.assertIn("A1", new)

    def test_every_feed_but_annuities_is_in_the_headlines_section(self):
        story = {"headlines": [{"date": "2026-11-13", "link": "https://example.com/a", "title": "t", "when": None}],
                 "verdict": "ok", "reason": "", "new": False}
        feeds = {f: {"stories": [story], "error": None, "capped": False, "available": 1} for f in b.FEEDS}
        with mock.patch.object(b, "headlines", return_value=(feeds, "")):
            page = b.block_news()
        self.assertIn('<h3 id="news-of-too-much-computing-power">News of too much computing power</h3>', page)
        self.assertNotIn("Annuity sales", page)


class LimraReleases(unittest.TestCase):
    SITEMAP = (b"<urlset><url><loc>https://www.limra.com/en/newsroom/news-releases/2026/q2-annuity-sales/</loc></url>"
               b"</urlset>")
    NEWSROOM = (b'<a href="/en/newsroom/news-releases/2026/q2-annuity-sales">Q2</a>'
                b'<a href="/en/newsroom/news-releases/2026/q3-annuity-sales-$126.3-billion">Q3</a>')

    def test_newsroom_page_adds_what_the_sitemap_lacks(self):
        pages = {b.LIMRA_SITEMAP: self.SITEMAP, b.LIMRA_NEWSROOM: self.NEWSROOM}
        with mock.patch.object(b, "fetch", side_effect=lambda url, **kw: pages[url]):
            urls, problem = b.limra_release_urls()
        self.assertIsNone(problem)
        self.assertIn("https://www.limra.com/en/newsroom/news-releases/2026/q3-annuity-sales-$126.3-billion", urls)

    def test_each_release_is_read_once_however_it_is_listed(self):
        pages = {b.LIMRA_SITEMAP: self.SITEMAP, b.LIMRA_NEWSROOM: self.NEWSROOM}
        read = []
        def release(url):
            read.append(url)
            return dict(released="2026-11-05", status="preliminary", quarters=[], definition_change=None)
        with mock.patch.object(b, "fetch", side_effect=lambda url, **kw: pages[url]), \
                mock.patch.object(b, "read_release", side_effect=release), \
                mock.patch.object(b, "load_json_store", return_value={}), mock.patch.object(b, "save_json_store"):
            b.limra_quarters()
        self.assertEqual(len(read), 2)  # Q2 once (listed with and without a final slash), and Q3

    def test_sitemap_alone_when_the_newsroom_page_is_down(self):
        def fetch(url, **kw):
            if url == b.LIMRA_NEWSROOM:
                raise RuntimeError("HTTP Error 503")
            return self.SITEMAP
        with mock.patch.object(b, "fetch", side_effect=fetch):
            urls, problem = b.limra_release_urls()
        self.assertEqual(len(urls), 1)
        self.assertIn("newsroom page couldn't be read today (HTTP Error 503)", problem)


class MarketImpliedRatings(unittest.TestCase):
    def test_grid_matches_the_articles_table(self):
        cases = {48: "A+", 85: "BBB+", 94: "BBB", 112: "BBB-", 129: "BBB-", 147: "BB+", 179: "BB", 266: "B+",
                 316: "B+", 1145: "CCC-", 10: "AAA"}
        for bp, rating in cases.items():
            self.assertEqual(b.NOTCHES[b.implied_notch(bp)], rating, bp)
        self.assertEqual(b.NOTCHES[b.implied_notch(134)], "BBB-")
        self.assertEqual(b.NOTCHES[b.implied_notch(135)], "BB+")

    def page(self, spreads, today=date(2026, 11, 20)):
        """spreads: {ticker: five-year spread in bp for November}, the rest of AGENCY_RATINGS without trades."""
        import math
        to_p = lambda bp: 1 - math.exp(-5 * b.cds_signal.hazard_from_spread(bp / 1e4))
        data = {t: ([b.cds_signal.Month(date(2026, 11, 1), to_p(bp), to_p(bp), to_p(bp), 10, False, 8,
                                        date(2026, 11, 10), date(2026, 11, 19), None)], {}, [])
                for t, bp in spreads.items()}
        with mock.patch.object(b, "cds_data", return_value=data), mock.patch.object(b, "TODAY", today), \
                mock.patch.object(b, "WARNINGS", []):
            return b.block_cds_ratings(), list(b.WARNINGS)

    def test_a_wide_gap_warns_and_names_the_fallen_angels(self):
        page, _ = self.page({"MSFT": 48, "META": 112, "ORCL": 316, "CRWV": 1145})
        self.assertIn("a warning today", page)
        self.assertIn("4 companies with recent trades average 4.8 notches below", page)
        self.assertIn("Oracle is rated investment grade but trades at BB or worse", page)
        self.assertIn("Alphabet, Amazon, Nvidia, AMD, Broadcom and SpaceX have no recent trades", page)

    def test_spreads_in_line_with_ratings_do_not(self):
        page, _ = self.page({"MSFT": 20, "ORCL": 120})
        self.assertIn("no warning today", page)
        self.assertIn("<td>none</td>", page)

    def test_a_name_just_past_the_line_is_not_listed(self):
        page, _ = self.page({"NVDA": 136, "ORCL": 316})
        self.assertIn(" Oracle is rated investment grade but trades at BB or worse.", page)
        self.assertNotIn("Nvidia is rated", page)

    def test_old_ratings_are_flagged(self):
        _, warnings = self.page({"MSFT": 48}, today=date(2027, 6, 1))
        self.assertTrue(any("update AGENCY_RATINGS" in w for w in warnings))


class OtherGuarantees(unittest.TestCase):
    def test_each_guarantor_gets_a_row(self):
        facts = {1652044: [{"end": "2025-06-30", "val": 2e9, "filed": "x"}, {"end": "2026-06-30", "val": 7.6e9, "filed": "x"}],
                 1730168: [{"end": "2026-08-02", "val": 29e9, "filed": "x"}]}
        with mock.patch.object(b, "sec_concept_or_empty", side_effect=lambda cik, c: facts[cik]):
            page = b.other_guarantees()
        self.assertIn("<td>Alphabet</td><td>$7.6B on 2026-06-30</td><td>$2.0B on 2025-06-30</td>", page)
        self.assertIn("<td>Broadcom</td><td>$29.0B on 2026-08-02</td><td>n/a</td>", page)


if __name__ == "__main__":
    unittest.main()
