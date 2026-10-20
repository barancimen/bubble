"""Tests for cds_signal.py. Run with: python3 -m unittest test_cds_signal (from cds/).

The documented-examples test reads the DTCC cache and is skipped when the cache isn't there."""
import io
import os
import unittest
import urllib.error
import zipfile
from datetime import date, timedelta
from unittest import mock

import cds_signal as c

D = date(2026, 3, 20)


class RescaledSpread(unittest.TestCase):
    def test_accepts_inside_neighbour_band(self):
        valid = [(D - timedelta(days=2), 0.0205), (D + timedelta(days=3), 0.0209)]
        self.assertAlmostEqual(c._rescaled_spread(0.000223, D, valid), 0.0223)

    def test_rejects_outside_band(self):
        # Oracle's recurring "50bp" prints while Oracle trades near 125bp.
        valid = [(D, 0.0120), (D, 0.0128)]
        self.assertIsNone(c._rescaled_spread(0.00005, D, valid))

    def test_rejects_without_nearby_quote(self):
        far = [(D + timedelta(days=c.RESCALE_DAYS + 1), 0.0042)]
        self.assertIsNone(c._rescaled_spread(0.000042, D, far))
        self.assertIsNone(c._rescaled_spread(0.000042, D, []))

    def test_ignores_normal_and_missing_values(self):
        valid = [(D, 0.02)]
        for s in (0.02, 0.0, -0.0002, float("nan")):
            self.assertIsNone(c._rescaled_spread(s, D, valid))


@unittest.skipUnless(os.path.isdir(c.CACHE) and os.listdir(c.CACHE), "no DTCC cache")
class DocumentedExamples(unittest.TestCase):
    """The three prints listed in CDS_VALIDATION.md. Only Oracle's has a properly reported quote
    within RESCALE_DAYS to confirm it; the other two stay out rather than being guessed."""

    @classmethod
    def setUpClass(cls):
        cls.hits = c.scan(date(2026, 10, 19))

    def decide(self, ticker, day, raw):
        trades = []
        for r in self.hits[ticker]:
            try:
                d = date.fromisoformat(r["Execution Timestamp"][:10])
                T = (date.fromisoformat(r["Expiration Date"]) - d).days / 365.25
                s = float(r.get("Spread-Leg 1") or "nan")
            except (KeyError, ValueError):
                continue
            if r.get("Action type") in ("NEWT", "CORR", "MODI") and 4 <= T <= 6:
                trades.append((d, s))
        self.assertIn((day, raw), trades, "example print not found in the cache")
        valid = [(d, s) for d, s in trades if 0.001 <= s <= 0.5]
        return c._rescaled_spread(raw, day, valid)

    def test_oracle_223bp_used(self):
        self.assertAlmostEqual(self.decide("ORCL", date(2026, 10, 2), 0.000223), 0.0223)

    def test_coreweave_450bp_unconfirmed(self):
        self.assertIsNone(self.decide("CRWV", date(2025, 10, 24), 0.00045))

    def test_nvidia_42bp_unconfirmed(self):
        self.assertIsNone(self.decide("NVDA", date(2026, 3, 20), 0.000042))


class Sync(unittest.TestCase):
    """Only a 404 on an old day is recorded as missing; other failures are retried."""

    def run_sync(self, error, tmp):
        today = date(2026, 10, 19)
        day = today - timedelta(days=14)  # a Monday, well past the 7-day grace period
        with mock.patch.object(c, "CACHE", tmp), \
                mock.patch.object(c, "MISSING", os.path.join(tmp, "missing.json")), \
                mock.patch.object(c, "WINDOW_DAYS", 14), \
                mock.patch("urllib.request.urlopen", side_effect=error):
            c.sync(today)
        with open(os.path.join(tmp, "missing.json")) as f:
            return day.isoformat() in f.read()

    def test_404_marks_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
            self.assertTrue(self.run_sync(err, tmp))

    def test_timeout_and_5xx_are_retried(self):
        import tempfile
        for err in (TimeoutError("timed out"), urllib.error.HTTPError("u", 503, "Unavailable", {}, None)):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertFalse(self.run_sync(err, tmp))

    def test_save_rejects_corrupt_zip(self):
        import tempfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("a.csv", "x,y\n1,2\n")
        good = buf.getvalue()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(c, "CACHE", tmp):
            c._save(D, good[:-10])  # truncated: starts with PK but isn't readable
            self.assertFalse(os.path.exists(c._path(D)))
            c._save(D, good)
            self.assertTrue(os.path.exists(c._path(D)))
            self.assertFalse(os.path.exists(c._path(D) + ".part"))


def trade_row(day, spread=None, paid=None, notional="1,000,000", coupon="0.05", years=5, n=[0], **extra):
    """A minimal DTCC row for resolve(): a new trade with either a quoted spread or a net cash payment."""
    n[0] += 1
    mat = date(day.year + years, 12 if day.month > 6 else 6, 20)
    row = {"Dissemination Identifier": str(n[0]), "Original Dissemination Identifier": "",
           "Action type": "NEWT", "Event type": "TRAD", "Execution Timestamp": f"{day}T15:00:00Z",
           "Effective Date": str(day + timedelta(days=1)), "Expiration Date": str(mat),
           "Fixed rate-Leg 1": coupon, "Notional currency-Leg 1": "USD", "Notional amount-Leg 1": notional,
           "Spread-Leg 1": "" if spread is None else str(spread),
           "Other payment amount": "" if paid is None else f"{paid:.5f}", "Other payment type": "UFRO",
           "file_date": str(day), "index_leg": False}
    row.update(extra)
    return row


def net_cash(spread, day, coupon=0.05, years=5):
    """|net cash| per 1,000,000 and who pays it, for a trade at a given par spread."""
    T = (date(day.year + years, 12 if day.month > 6 else 6, 20) - day).days / 365.25
    u = c._flat_upfront(c._implied_hazard(0, T, spread), T, coupon)
    n = u - c._accrued(day, coupon)
    return abs(n) * 1e6, (1 if n > 0 else -1)


class Accrual(unittest.TestCase):
    def test_coupon_dates_roll_to_monday(self):
        self.assertEqual(c._imm(2026, 9), date(2026, 9, 21))   # Sep 20, 2026 is a Sunday
        self.assertEqual(c._imm(2026, 6), date(2026, 6, 22))   # Jun 20, 2026 is a Saturday
        self.assertEqual(c._imm(2026, 3), date(2026, 3, 20))

    def test_accrued_restarts_at_step_in(self):
        self.assertEqual(c._accrued(date(2026, 9, 20), 0.05), 0)  # step-in Sep 21 is the coupon date
        self.assertAlmostEqual(c._accrued(date(2026, 9, 18), 0.05), 0.05 * 89 / 360)  # from Jun 22 to Sep 19


class NetCash(unittest.TestCase):
    """DTCC's payment is the clean up-front minus the accrued coupon; a quote settles who paid."""

    def test_recovers_the_spread_on_either_side_of_the_coupon(self):
        start = date(2026, 1, 1)
        for spread in (0.08, 0.035):  # buyer pays at 800bp, seller pays at 350bp (coupon 500bp)
            day = date(2026, 8, 12)
            paid, _ = net_cash(spread, day)
            rows = [trade_row(day, spread=spread), trade_row(day, paid=paid)]
            priced = c.resolve(rows, start)
            spreads = sorted(c.par_spread(t["hazard"], t["T"]) for t in priced)
            for s in spreads:
                self.assertAlmostEqual(s, spread, places=4)

    def test_impossible_reading_settles_the_trade(self):
        # Oracle-like: 1% coupon at 270bp. If the seller had paid this much, the spread would be negative.
        day, start = date(2026, 10, 14), date(2026, 1, 1)
        paid, side = net_cash(0.027, day, coupon=0.01)
        self.assertEqual(side, 1)
        (t,) = c.resolve([trade_row(day, paid=paid, coupon="0.01")], start)
        self.assertTrue(t["anchored"])
        self.assertAlmostEqual(c.par_spread(t["hazard"], t["T"]), 0.027, places=4)

    def test_no_evidence_leaves_both_readings(self):
        day, start = date(2026, 5, 13), date(2026, 1, 1)
        paid, _ = net_cash(0.045, day)  # 450bp: both readings are possible
        (t,) = c.resolve([trade_row(day, paid=paid)], start)
        self.assertFalse(t["anchored"])
        readings = sorted(c.par_spread(h, t["T"]) for h in (t["hazard"], t["hazard_alt"]))
        self.assertAlmostEqual(readings[0], 0.045, places=4)


class CouponDateTest(unittest.TestCase):
    def test_true_side_runs_straight_through_the_coupon_date(self):
        start = date(2026, 1, 1)
        for spread in (0.08, 0.04):
            rows = []
            for day in (date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18), date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)):
                paid, _ = net_cash(spread, day)
                rows.append(trade_row(day, paid=paid))
            priced = c.resolve(rows, start)
            self.assertTrue(all(t["anchored"] for t in priced))
            for t in priced:
                self.assertAlmostEqual(c.par_spread(t["hazard"], t["T"]), spread, places=4)


class Cleaning(unittest.TestCase):
    def test_lifecycle_records_are_not_prices(self):
        day, start = date(2026, 8, 10), date(2026, 1, 1)
        rows = [trade_row(day, spread=0.008, coupon="0.01"),
                trade_row(day, paid=130818.14, coupon="0.01", **{"Event type": "NOVA"}),
                trade_row(day, paid=130818.14, coupon="0.01", **{"Action type": "MODI"})]
        self.assertEqual(len(c.resolve(rows, start)), 1)

    def test_isolated_bad_print_is_dropped(self):
        start = date(2026, 1, 1)
        rows = [trade_row(date(2026, 8, d), spread=0.0072, coupon="0.01") for d in (4, 5, 6, 7)]
        rows.append(trade_row(date(2026, 8, 6), spread=0.026, coupon="0.01"))  # 3.6x its neighbours
        self.assertEqual(len(c.resolve(rows, start)), 4)

    def test_entity_matching(self):
        header = ["Underlier ID-Leg 1", "Underlier ID source-Leg 1", "Underlying Asset Name", "UPI Underlier Name"]
        idx = {h: i for i, h in enumerate(header)}
        self.assertEqual(c._entity(["6EC42LAD6", "REDID", "", "NT"], idx), "ORCL")
        self.assertIsNone(c._entity(["6EC42I", "REDID", "ORACLE SYSTEMS CORP", "NT"], idx))
        self.assertEqual(c._entity(["US21873SAC26", "ISIN", "", "SR NT 144A"], idx), "CRWV")
        self.assertIsNone(c._entity(["LX289889", "SingleOther", "COREWEAVE FINANCING-DDTL", ""], idx))
        self.assertEqual(c._entity(["", "", "NVIDIA CORPORATION", ""], idx), "NVDA")

    def test_index_legs_are_flagged(self):
        import csv as csvmod
        import tempfile
        header = ["Dissemination Identifier", "Execution Timestamp", "Underlier ID-Leg 1", "Underlier ID source-Leg 1",
                  "Underlying Asset Name", "UPI Underlier Name", "Other payment amount"]
        lines = [header] + [[str(i), "2026-10-14T15:00:00Z", f"X{i:05d}", "REDID", f"COMPANY {i}", "NT", "23665.00000"] for i in range(124)]
        lines += [["900", "2026-10-14T15:00:00Z", "6EC42L", "REDID", "ORACLE CORPORATION", "NT", "23665.00000"],
                  ["901", "2026-10-14T16:00:00Z", "6EC42L", "REDID", "ORACLE CORPORATION", "NT", "52345.00000"]]
        buf = io.StringIO()
        csvmod.writer(buf, quoting=csvmod.QUOTE_ALL).writerows(lines)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(c, "CACHE", tmp):
            with zipfile.ZipFile(os.path.join(tmp, "SEC_CUMULATIVE_CREDITS_2026_10_14.zip"), "w") as z:
                z.writestr("x.csv", buf.getvalue())
            hits = c.scan(date(2026, 10, 19))
        flags = {r["Dissemination Identifier"]: r["index_leg"] for r in hits["ORCL"]}
        self.assertEqual(flags, {"900": True, "901": False})


class Monthly(unittest.TestCase):
    def test_split_block_counts_once_and_unresolved_months_show_both(self):
        start, end = date(2026, 8, 1), date(2026, 9, 30)
        pts = [(date(2026, 8, 3), 0.40, True, 0.40)] * 3 + [(date(2026, 8, 4), 0.42, True, 0.42),
                                                              (date(2026, 8, 5), 0.44, True, 0.44)]
        pts += [(date(2026, 9, 3), 0.45, False, 0.20), (date(2026, 9, 4), 0.47, False, 0.22)]
        aug, sep = c.monthly(pts, start, end)
        self.assertEqual((aug.n, aug.days, aug.median, aug.unresolved), (3, 3, 0.42, False))
        self.assertTrue(sep.unresolved)
        self.assertAlmostEqual(sep.median, 0.46)
        self.assertAlmostEqual(sep.alt, 0.21)


if __name__ == "__main__":
    unittest.main()
