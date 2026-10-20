"""Tests for bubble_watch.py (LIMRA checks, spreads, weak borrowers, Oracle prepayments, stores). Run with: python3 -m unittest test_bubble_watch"""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

import bubble_watch as b

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
        with mock.patch.object(b, "fetch", return_value=csv_text.encode()):
            rows = b.fred_series("BAMLC0A4CBBB")
        self.assertEqual(rows[-1][1] - min(v for _, v in rows[-366:]), 30)
        first, episodes = b.spread_episodes(rows, 30)
        self.assertEqual(len(episodes), 1)


class WeakBorrowers(unittest.TestCase):
    def test_falling_together_while_market_holds_up_warns(self):
        def closes(ticker, span="1y"):
            start = 100.0
            end = 99.0 if ticker in ("QQQ", "ORCL") else 70.0
            return [(str(date(2026, 1, 1) + timedelta(days=i)), start if i < 60 else end) for i in range(80)]
        with mock.patch.object(b, "yahoo_closes", side_effect=closes):
            self.assertIn("WARNING", b.block_weak_borrowers())
        def calm(ticker, span="1y"):
            return [(str(date(2026, 1, 1) + timedelta(days=i)), 100.0) for i in range(80)]
        with mock.patch.object(b, "yahoo_closes", side_effect=calm):
            self.assertIn("No warning", b.block_weak_borrowers())


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
        with mock.patch.object(b, "sec_concept", return_value=facts):
            page = b.block_nvidia()
        self.assertIn("Check by hand", page)
        self.assertNotIn("WARNING", page)


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


if __name__ == "__main__":
    unittest.main()
