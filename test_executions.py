import unittest
from unittest.mock import patch

import master_odds_parser as parser


class ExecutionTests(unittest.TestCase):
    def test_discovery_issues_do_not_prevent_exporting_resolved_matches(self):
        def discover(*args, **kwargs):
            kwargs["issues"].append("[LDC] Fenerbahce - Slavia Prague : BETCLIC introuvable")
            return [{"event_pmu": 1, "event_wina": 2, "url_betclic": "",
                     "home": "Available", "away": "Match"}]

        with patch.object(parser, "configure_logging"), \
                patch.object(parser, "get_daily_matches", side_effect=discover), \
                patch.object(parser, "process_single_match", return_value="exported") as process, \
                patch.object(parser, "export_execution") as export, self.assertRaises(SystemExit):
            parser.main([])
        process.assert_called_once()
        summary = export.call_args.args[0]
        self.assertEqual((summary["failed"], summary["exported"]), (1, 1))
        self.assertEqual(summary["status"], "Termine avec erreurs")
        self.assertIn("Exporte : Available - Match", summary["detail"])
        self.assertIn("Erreur decouverte : [LDC]", summary["detail"])

    def test_discovery_issues_are_reported_even_without_eligible_matches(self):
        def discover(*args, **kwargs):
            kwargs["issues"].append("BETCLIC missing")
            return []

        with patch.object(parser, "configure_logging"), \
                patch.object(parser, "get_daily_matches", side_effect=discover), \
                patch.object(parser, "export_execution") as export, self.assertRaises(SystemExit):
            parser.main([])
        self.assertEqual(export.call_args.args[0]["failed"], 1)
        self.assertIn("BETCLIC missing", export.call_args.args[0]["detail"])

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

    def test_failed_first_match_does_not_stop_queued_matches_and_details_name_each_outcome(self):
        records = [
            {"event_pmu": i, "event_wina": i, "url_betclic": "",
             "home": f"Home {i}", "away": f"Away {i}"}
            for i in range(1, 5)
        ]

        def worker(event, *args, **kwargs):
            if event == 1:
                raise ValueError("basket incomplete")
            return "exported"

        with patch.object(parser, "configure_logging"), \
                patch.object(parser, "get_daily_matches", return_value=records), \
                patch.object(parser, "process_single_match", side_effect=worker) as process, \
                patch.object(parser, "export_execution") as export, \
                self.assertLogs(level="ERROR"), self.assertRaises(SystemExit):
            parser.main([])
        self.assertEqual(process.call_count, 4)
        summary = export.call_args.args[0]
        self.assertEqual((summary["exported"], summary["failed"]), (3, 1))
        self.assertIn("Erreur : Home 1 - Away 1 : ValueError: basket incomplete", summary["detail"])
        for i in range(2, 5):
            self.assertIn(f"Exporte : Home {i} - Away {i}", summary["detail"])

    def test_manual_matches_without_team_names_use_bookmaker_ids(self):
        records = [{"event_pmu": 123, "event_wina": 456, "url_betclic": ""}]
        with patch.object(parser, "get_daily_matches", return_value=records), \
                patch.object(parser, "process_single_match", return_value="exported"):
            summary = dict(exported=0, ignored=0, failed=0, detail="")
            parser.run_matches(parser.parse_args([]), summary)
        self.assertEqual(summary["detail"], "Exporte : PMU 123 / Winamax 456\n")

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
            sheet.col_count = 11
            sheet.row_values.return_value = []
            parser.export_execution(summary)
        self.assertEqual(book.add_worksheet.call_args.kwargs["title"], "Exécutions")
        self.assertEqual(sheet.append_row.call_count, 2)
        row = sheet.append_row.call_args.args[0]
        self.assertEqual(row[2:7], ["l1", "Termine", 2, 3, 0])
        self.assertEqual(sheet.append_row.call_args.kwargs["value_input_option"], "RAW")
        sheet.sort.assert_called_once_with(
            (1, "des"), (2, "des"), range="A2:H" + str(sheet.row_count),
        )

    def test_deferred_only_run_is_kept_in_history(self):
        summary = dict(start="2026-10-20 18:05:00", competitions="ldc", status="Termine",
                       exported=0, ignored=1, failed=0, deferred=1,
                       detail="Differe (cotes incompletes) : A - B : Cote Wina : 1/20\n")
        with patch.object(parser.gspread, "service_account") as auth:
            sheet = auth.return_value.open.return_value.worksheet.return_value
            sheet.col_count = 11
            sheet.row_values.return_value = [
                "Debut (Paris)", "Fin (Paris)", "Competitions", "Statut",
                "Matchs exportes", "Matchs ignores", "Matchs en erreur", "Detail",
            ]
            parser.export_execution(summary)
        sheet.append_row.assert_called_once()
        self.assertIn("Differe", sheet.append_row.call_args.args[0][7])

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
            sheet.col_count = 8
            sheet.row_values.return_value = [
                "Debut (Paris)", "Fin (Paris)", "Competitions", "Statut",
                "Matchs exportes", "Matchs ignores", "Matchs en erreur", "Detail",
            ]
            parser.export_execution(summary)
        book.add_worksheet.assert_not_called()
        sheet.append_row.assert_not_called()
        sheet.sort.assert_not_called()
        sheet.resize.assert_called_once_with(cols=11)
        self.assertEqual(sheet.update.call_args.kwargs["range_name"], "J2:K10")
        values = sheet.update.call_args.kwargs["values"]
        self.assertEqual(values[0], ["Dernier passage", ""])
        self.assertEqual(values[4], ["Statut", "Aucun match exporte"])
        sheet.clear.assert_not_called()

    def test_error_without_export_is_still_appended(self):
        summary = dict(start="2026-10-15 18:30:00", competitions="pl",
                       status="Termine avec erreurs", exported=0, ignored=0, failed=0,
                       detail="ValueError: discovery")
        with patch.object(parser.gspread, "service_account") as auth:
            sheet = auth.return_value.open.return_value.worksheet.return_value
            sheet.col_count = 11
            sheet.row_values.return_value = [
                "Debut (Paris)", "Fin (Paris)", "Competitions", "Statut",
                "Matchs exportes", "Matchs ignores", "Matchs en erreur", "Detail",
            ]
            parser.export_execution(summary)
        sheet.append_row.assert_called_once()
        self.assertEqual(sheet.append_row.call_args.kwargs["table_range"], "A:H")
        sheet.update.assert_called_once()
        sheet.sort.assert_called_once()


if __name__ == "__main__":
    unittest.main()
