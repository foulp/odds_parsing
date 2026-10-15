import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock

from page_diagnostics import capture_market_diagnostic


class PageDiagnosticTests(unittest.TestCase):
    def test_saves_page_state_and_screenshot_without_exposing_unrelated_data(self):
        page = MagicMock()
        page.evaluate.return_value = {
            "url": "https://example.test/match", "title": "Match",
            "readyState": "complete", "hasMarketText": True,
            "headings": ["Joueur décisif"], "textStart": "public page",
            "textEnd": "public page",
        }
        with TemporaryDirectory() as directory, self.assertLogs(level="WARNING") as logs:
            result = capture_market_diagnostic(
                page, "winamax", 123, "Joueur décisif", output_dir=directory,
            )
            saved = json.loads((Path(directory) / "winamax-match-123.json").read_text())
            self.assertEqual(saved, result)
            self.assertTrue(page.screenshot.call_args.kwargs["full_page"])
        self.assertIn('"hasMarketText": true', " ".join(logs.output))

    def test_screenshot_failure_does_not_hide_saved_diagnostic(self):
        page = MagicMock()
        page.evaluate.return_value = {"url": "https://example.test/match"}
        page.screenshot.side_effect = OSError("disk unavailable")
        with TemporaryDirectory() as directory, self.assertLogs(level="WARNING") as logs:
            capture_market_diagnostic(
                page, "betclic", "match/123", "SuperSub", output_dir=directory,
            )
            self.assertTrue((Path(directory) / "betclic-match-match-123.json").exists())
        self.assertIn("Impossible d'enregistrer la capture", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
