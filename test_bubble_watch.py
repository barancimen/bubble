"""Tests for the LIMRA parsing checks in bubble_watch.py. Run with: python3 -m unittest test_bubble_watch"""
import unittest
from datetime import date

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


if __name__ == "__main__":
    unittest.main()
