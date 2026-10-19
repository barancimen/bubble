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


if __name__ == "__main__":
    unittest.main()
