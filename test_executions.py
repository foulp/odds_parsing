import unittest
from unittest.mock import patch

import master_odds_parser as parser


class ExecutionTests(unittest.TestCase):
    def test_empty_run_is_recorded(self):
        with patch.object(parser, "configure_logging"), \
                patch.object(parser, "get_daily_matches", return_value=[]), \
                patch.object(parser, "export_execution") as export:
            parser.main(["-c", "l1"])
        summary = export.call_args.args[0]
        self.assertEqual(summary["status"], "Aucun match exporte")
        self.assertEqual(summary["competitions"], "l1")
        self.assertEqual(summary["exported"], 0)

    def test_discovery_error_is_recorded_and_propagates(self):
        with patch.object(parser, "configure_logging"), \
                patch.object(parser, "get_daily_matches", side_effect=ValueError("discovery")), \
                patch.object(parser, "export_execution") as export, \
                self.assertRaisesRegex(ValueError, "discovery"):
            parser.main([])
        self.assertEqual(export.call_args.args[0]["status"], "Termine avec erreurs")
        self.assertIn("discovery", export.call_args.args[0]["detail"])

    def test_exported_ignored_and_failed_matches_are_counted(self):
        records = [{"event_pmu": i, "event_wina": i, "url_betclic": ""} for i in (1, 2, 3)]
        with patch.object(parser, "configure_logging"), \
                patch.object(parser, "get_daily_matches", return_value=records), \
                patch.object(parser, "process_single_match",
                             side_effect=["exported", "ignored", ValueError("match error")]), \
                patch.object(parser, "export_execution") as export, \
                self.assertLogs(level="ERROR"), self.assertRaises(SystemExit):
            parser.main([])
        summary = export.call_args.args[0]
        self.assertEqual((summary["exported"], summary["ignored"], summary["failed"]), (1, 1, 1))
        self.assertEqual(summary["status"], "Termine avec erreurs")

    def test_sheet_failure_is_reported(self):
        with patch.object(parser, "configure_logging"), \
                patch.object(parser, "get_daily_matches", return_value=[]), \
                patch.object(parser, "export_execution", side_effect=RuntimeError("quota")), \
                self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "quota"):
            parser.main([])

    def test_summary_failure_does_not_mask_original_error(self):
        with patch.object(parser, "configure_logging"), \
                patch.object(parser, "get_daily_matches", side_effect=ValueError("original")), \
                patch.object(parser, "export_execution", side_effect=RuntimeError("quota")), \
                self.assertLogs(level="ERROR"), self.assertRaisesRegex(ValueError, "original"):
            parser.main([])

    def test_creates_tab_and_appends_raw_summary(self):
        summary = dict(start="2026-10-15 18:30:00", competitions="l1",
                       status="Termine", exported=2, ignored=3, failed=0, detail="")
        with patch.object(parser.gspread, "service_account") as auth:
            book = auth.return_value.open.return_value
            book.worksheet.side_effect = parser.gspread.exceptions.WorksheetNotFound("missing")
            sheet = book.add_worksheet.return_value
            sheet.row_values.return_value = []
            parser.export_execution(summary)
        self.assertEqual(book.add_worksheet.call_args.kwargs["title"], "Exécutions")
        self.assertEqual(sheet.append_row.call_count, 2)
        row = sheet.append_row.call_args.args[0]
        self.assertEqual(row[2:7], ["l1", "Termine", 2, 3, 0])
        self.assertEqual(sheet.append_row.call_args.kwargs["value_input_option"], "RAW")

    def test_incompatible_header_is_not_overwritten(self):
        with patch.object(parser.gspread, "service_account") as auth:
            sheet = auth.return_value.open.return_value.worksheet.return_value
            sheet.row_values.return_value = ["Other data"]
            with self.assertRaisesRegex(ValueError, "incompatibles"):
                parser.export_execution({})
            sheet.append_row.assert_not_called()

    def test_existing_tab_keeps_previous_rows(self):
        summary = dict(start="2026-10-15 18:30:00", competitions="pl",
                       status="Aucun match exporte", exported=0, ignored=2, failed=0, detail="")
        with patch.object(parser.gspread, "service_account") as auth:
            book = auth.return_value.open.return_value
            sheet = book.worksheet.return_value
            sheet.row_values.return_value = [
                "Debut (Paris)", "Fin (Paris)", "Competitions", "Statut",
                "Matchs exportes", "Matchs ignores", "Matchs en erreur", "Detail",
            ]
            parser.export_execution(summary)
        book.add_worksheet.assert_not_called()
        sheet.append_row.assert_called_once()
        sheet.clear.assert_not_called()


if __name__ == "__main__":
    unittest.main()
