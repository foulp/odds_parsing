from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import master_odds_parser as parser
import scheduled_runs
import sheets_cleanup as cleanup


TODAY = date(2026, 10, 19)


class SheetsCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        patcher = patch.object(scheduled_runs, "CACHE_DIR", self.directory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.book = Mock(id="book")
        self.open_book = Mock(return_value=self.book)

    def track(self, sheet_id, title, day, created=True):
        cleanup.track_match_sheet("book", sheet_id, title,
                                 f"2026-10-{day:02d}T21:00:00+02:00", created)

    def test_only_owned_previous_day_sheets_are_deleted(self):
        old = Mock(id=1, title="Old match")
        today = Mock(id=2, title="Today")
        future = Mock(id=3, title="Future")
        legacy = Mock(id=4, title="Legacy match")
        executions = Mock(id=5, title="Exécutions")
        self.book.worksheets.return_value = [old, today, future, legacy, executions]
        self.track(1, old.title, 18)
        self.track(2, today.title, 19)
        self.track(3, future.title, 20)
        cleanup.cleanup_match_sheets(self.open_book, TODAY)
        self.book.del_worksheet.assert_called_once_with(old)
        self.assertNotIn("1", cleanup.read_state()["books"]["book"]["sheets"])
        cleanup.cleanup_match_sheets(self.open_book, TODAY)
        self.open_book.assert_called_once()

    def test_legacy_sheets_are_not_adopted_by_a_new_export(self):
        self.track(1, "Legacy", 18, created=False)
        self.assertFalse((self.directory / "sheets-cleanup.json").exists())

    def test_new_export_updates_owned_sheet_and_preserves_latest_kickoff(self):
        self.track(1, "Match", 18)
        self.track(1, "Match", 20, created=False)
        self.track(1, "Match", 19, created=False)
        self.book.worksheets.return_value = [Mock(id=1, title="Match")]
        cleanup.cleanup_match_sheets(self.open_book, TODAY)
        self.book.del_worksheet.assert_not_called()

    def test_midnight_uses_paris_date(self):
        cleanup.track_match_sheet("book", 1, "Late match", "2026-10-18T23:30:00Z", True)
        self.book.worksheets.return_value = [Mock(id=1, title="Late match")]
        cleanup.cleanup_match_sheets(self.open_book, TODAY)
        self.book.del_worksheet.assert_not_called()

    def test_renamed_sheets_are_preserved(self):
        self.track(1, "Match", 18)
        self.book.worksheets.return_value = [Mock(id=1, title="Exécutions")]
        with self.assertLogs(level="WARNING"):
            cleanup.cleanup_match_sheets(self.open_book, TODAY)
        self.book.del_worksheet.assert_not_called()

    def test_missing_tabs_are_removed_from_state_without_google_delete(self):
        self.track(1, "Match", 18)
        self.book.worksheets.return_value = []
        cleanup.cleanup_match_sheets(self.open_book, TODAY)
        self.book.del_worksheet.assert_not_called()
        self.assertEqual(cleanup.read_state()["books"]["book"]["sheets"], {})

    def test_failed_delete_is_retried_and_daily_marker_not_saved(self):
        self.track(1, "Match", 18)
        self.book.worksheets.return_value = [Mock(id=1, title="Match")]
        self.book.del_worksheet.side_effect = RuntimeError("quota")
        with self.assertRaisesRegex(RuntimeError, "quota"):
            cleanup.cleanup_match_sheets(self.open_book, TODAY)
        self.assertNotIn("last_cleanup", cleanup.read_state())
        self.book.del_worksheet.side_effect = None
        cleanup.cleanup_match_sheets(self.open_book, TODAY)
        self.assertEqual(self.book.del_worksheet.call_count, 2)

    def test_next_day_runs_cleanup_again(self):
        self.track(1, "Match", 19)
        sheet = Mock(id=1, title="Match")
        self.book.worksheets.return_value = [sheet]
        cleanup.cleanup_match_sheets(self.open_book, TODAY)
        cleanup.cleanup_match_sheets(self.open_book, date(2026, 10, 20))
        self.book.del_worksheet.assert_called_once_with(sheet)
        self.assertEqual(self.open_book.call_count, 2)

    def test_invalid_state_aborts_before_accessing_google(self):
        (self.directory / "sheets-cleanup.json").write_text('{"version": 2}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalide"):
            cleanup.cleanup_match_sheets(self.open_book, TODAY)
        self.open_book.assert_not_called()

    def test_execution_failure_is_recorded_and_propagated(self):
        with patch.object(parser, "configure_logging"), \
                patch.object(parser, "cleanup_match_sheets", side_effect=RuntimeError("cleanup")), \
                patch.object(parser, "run_matches") as run, \
                patch.object(parser, "export_execution") as export, \
                self.assertRaisesRegex(RuntimeError, "cleanup"):
            parser.main(["--scheduled"])
        run.assert_not_called()
        self.assertEqual(export.call_args.args[0]["status"], "Termine avec erreurs")
        self.assertIn("cleanup", export.call_args.args[0]["detail"])


if __name__ == "__main__":
    unittest.main()
