from datetime import date, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

import match_discovery as discovery
import master_odds_parser as parser


DAY = date(2026, 10, 15)


def fixture(home="PSG", away="Marseille", competition="l1"):
    return {
        "competition": competition, "home": home, "away": away,
        "start": "2026-10-15T21:00:00+02:00", "flashscore_url": "https://fixture.test/match",
        "event_pmu": 123, "event_wina": 456, "url_betclic": "https://betclic.test/match-m123",
    }


class SelectionTests(unittest.TestCase):
    def test_date_defaults_to_today_and_accepts_tomorrow_or_iso(self):
        self.assertIsNone(parser.parse_args([]).date)
        self.assertEqual(parser.parse_args(["--date", "2026-10-16"]).date, date(2026, 10, 16))
        with patch.object(parser, "datetime") as clock:
            clock.now.return_value = datetime(2026, 12, 31, 23, 30, tzinfo=discovery.PARIS)
            self.assertEqual(parser.parse_args(["--date", "demain"]).date, date(2027, 1, 1))
            clock.now.assert_called_with(discovery.PARIS)
        for arguments in (
            ["--date", "2026-02-30"], ["--date", "16/10/2026"],
            ["--date", "20261016"], ["--file", "matchs.csv", "--date", "demain"],
        ):
            with self.subTest(arguments=arguments), patch("sys.stderr"), self.assertRaises(SystemExit):
                parser.parse_args(arguments)

    def test_cli_filters_accept_multiple_values_and_cote_out_keeps_long_form(self):
        args = parser.parse_args(["-c", "l1", "pl", "-m", "PSG - Marseille",
                                  "Arsenal - Chelsea", "--cote-out", "3"])
        self.assertEqual(args.competition, ["l1", "pl"])
        self.assertEqual(args.match, ["PSG - Marseille", "Arsenal - Chelsea"])
        self.assertEqual(args.cote_out, 3)
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            parser.parse_args(["-c", "4"])

    def test_match_queries_are_unique_deduplicated_and_all_validated(self):
        a, b = fixture(), fixture("Arsenal", "Chelsea", "pl")
        self.assertEqual(discovery.select_matches([a, b], ["PSG", "Arsenal - Chelsea", "PSG"]), [a, b])
        self.assertEqual(discovery.select_matches([a], ["Paris SG - Marseille"]), [a])
        for records, query in (([a, dict(a, competition="ldc")], "PSG"), ([a], "Unknown")):
            with self.assertRaisesRegex(ValueError, "correspondances"):
                discovery.select_matches(records, [query])

    def test_association_requires_correct_sides_time_and_unique_winner(self):
        ref = fixture()
        candidate = dict(ref, home="Paris SG", id=1)
        self.assertEqual(discovery.associate_match(ref, [candidate], "PMU"), candidate)
        for candidates in (
            [dict(candidate, home="Marseille", away="PSG")],
            [dict(candidate, start="2026-10-15T22:00:00+02:00")],
            [dict(candidate, start="2026-10-16T21:00:00+02:00")],
            [candidate, dict(candidate, id=2)],
        ):
            with self.subTest(candidates=candidates), self.assertRaises(ValueError):
                discovery.associate_match(ref, candidates, "PMU")

    def test_paris_dates_use_offset_not_utc_day(self):
        self.assertEqual(discovery.local_start("2026-10-14T23:30:00Z").date(), DAY)
        with self.assertRaisesRegex(ValueError, "fuseau"):
            discovery.local_start("2026-10-15T21:00:00")

    def test_betclic_relative_numeric_and_french_dates(self):
        now = datetime(2026, 10, 15, tzinfo=discovery.PARIS)
        for label, expected in (("Aujourd'hui", DAY), ("Demain", date(2026, 10, 16)),
                                ("Sam. 17/10", date(2026, 10, 17)),
                                ("samedi 17 octobre", date(2026, 10, 17))):
            self.assertEqual(discovery.betclic_date(label, now), expected)
        with self.assertRaisesRegex(ValueError, "non reconnue"):
            discovery.betclic_date("Plus tard", now)


class AdapterTests(unittest.TestCase):
    def test_no_fixtures_skips_all_bookmaker_requests(self):
        with patch.object(discovery, "discover_matches", return_value=[]), \
                patch.object(discovery, "read_pmu_matches") as pmu, \
                patch.object(discovery, "read_winamax_matches") as wina, \
                patch.object(discovery, "read_betclic_matches") as betclic:
            self.assertEqual(discovery.discover_competition(MagicMock(), MagicMock(), "l1", DAY), [])
        pmu.assert_not_called()
        wina.assert_not_called()
        betclic.assert_not_called()

    def test_discovery_joins_references_and_rejects_reused_bookmaker_id(self):
        fs = [{"home": "PSG", "away": "Marseille", "start": fixture()["start"],
               "url": fixture()["flashscore_url"]}]
        candidate = {"home": "PSG", "away": "Marseille", "start": fixture()["start"], "id": 123}
        with patch.object(discovery, "discover_matches", return_value=fs), \
                patch.object(discovery, "read_pmu_matches", return_value=[candidate]), \
                patch.object(discovery, "read_winamax_matches", return_value=[dict(candidate, id=456)]), \
                patch.object(discovery, "read_betclic_matches",
                             return_value=[dict(candidate, url=fixture()["url_betclic"])]):
            result = discovery.discover_competition(MagicMock(), MagicMock(), "l1", DAY)
            self.assertEqual(result, [fixture()])
            fs.append(dict(fs[0]))
            with self.assertRaisesRegex(ValueError, "plusieurs rencontres"):
                discovery.discover_competition(MagicMock(), MagicMock(), "l1", DAY)

    def test_pmu_filters_day_and_ignores_outrights(self):
        session = MagicMock()
        session.get.return_value.json.return_value = {"events": [
            {"event": {"id": i, "homeName": "PSG", "awayName": "Marseille",
                       "start": start, "tags": tags}}
            for i, start, tags in (
                (1, "2026-10-14T23:30:00Z", ["MATCH"]),
                (2, "2026-10-16T12:00:00Z", ["MATCH"]),
                (3, "2026-10-15T12:00:00Z", ["OUTRIGHT"]),
            )
        ]}
        records = discovery.read_pmu_matches(session, "l1", DAY)
        self.assertEqual([r["id"] for r in records], [1])
        session.get.return_value.raise_for_status.assert_called_once()

    def test_winamax_filters_today_and_requests_exact_tournament(self):
        page = MagicMock()
        page.evaluate.return_value = [
            {"matchId": i, "competitor1Name": "PSG", "competitor2Name": "Marseille",
             "matchStart": datetime(2026, 10, day, 21, tzinfo=discovery.PARIS).timestamp()}
            for i, day in ((1, 15), (2, 16))
        ]
        self.assertEqual([r["id"] for r in discovery.read_winamax_matches(page, "l1", DAY)], [1])
        self.assertEqual(page.evaluate.call_args.args[1], 4)

    def test_winamax_timeout_reports_page_diagnostic_and_stops(self):
        page = MagicMock()
        page.goto.return_value.status = 403
        page.wait_for_function.side_effect = discovery.PlaywrightTimeoutError("timeout")
        diagnostic = {
            "url": "https://www.winamax.fr/blocked", "title": "Access denied",
            "readyState": "complete", "hasState": False, "hasTournament": False,
            "hasMatches": False, "text": "Access denied",
        }
        page.evaluate.return_value = diagnostic
        with TemporaryDirectory() as directory, \
                patch.object(discovery, "Path", return_value=Path(directory)), \
                self.assertLogs(level="ERROR") as logs, \
                self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            discovery.read_winamax_matches(page, "l1", DAY)
        self.assertIn("Access denied", " ".join(logs.output))
        self.assertEqual(page.evaluate.call_count, 1)

    def test_winamax_timeout_persists_diagnostic_without_assuming_http_block(self):
        page = MagicMock()
        page.goto.return_value = None
        page.wait_for_function.side_effect = discovery.PlaywrightTimeoutError("timeout")
        page.evaluate.return_value = {"title": "Paris sportifs", "hasState": False}
        with TemporaryDirectory() as directory, \
                patch.object(discovery, "Path", return_value=Path(directory)), \
                self.assertLogs(level="ERROR"), \
                self.assertRaisesRegex(RuntimeError, "chargement incomplet"):
            try:
                discovery.read_winamax_matches(page, "l1", DAY)
            finally:
                saved = json.loads((Path(directory) / "winamax-l1.json").read_text())
                self.assertIsNone(saved["status"])
                self.assertEqual(saved["title"], "Paris sportifs")

    def test_betclic_reads_team_sides_and_validates_date(self):
        page = MagicMock()
        page.locator.return_value.evaluate_all.return_value = [
            {"title": "PSG - Marseille", "url": "https://betclic.fr/a-m123",
             "date": "Jeu. 15/10", "time": "21:00"},
            {"title": "PSG - Other", "url": "https://betclic.fr/a-m456",
             "date": "Ven. 16/10", "time": "21:00"},
        ]
        records = discovery.read_betclic_matches(page, "l1", DAY)
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]["home"], records[0]["away"]), ("PSG", "Marseille"))


class CacheTests(unittest.TestCase):
    def test_refresh_only_rebuilds_selected_competitions(self):
        with TemporaryDirectory() as directory, patch.object(discovery, "discovery_page"), \
                patch.object(discovery, "discover_competition", return_value=[]) as discover:
            path = Path(directory) / f"{DAY}.json"
            path.write_text(json.dumps({
                "version": 1, "date": str(DAY),
                "competitions": {"l1": [fixture()], "pl": []},
            }))
            discovery.get_daily_matches(["pl"], MagicMock(), today=DAY,
                                        refresh=True, cache_dir=directory)
            payload = json.loads(path.read_text())
            self.assertEqual(payload["competitions"]["l1"], [fixture()])
            self.assertEqual(discover.call_count, 1)
            self.assertEqual(discover.call_args.args[2], "pl")

    def test_new_day_uses_a_different_cache_file(self):
        with TemporaryDirectory() as directory, patch.object(discovery, "discovery_page"), \
                patch.object(discovery, "discover_competition", return_value=[]) as discover:
            discovery.get_daily_matches(["l1"], MagicMock(), today=DAY, cache_dir=directory)
            discovery.get_daily_matches(["l1"], MagicMock(), today=date(2026, 10, 16), cache_dir=directory)
            self.assertEqual(discover.call_count, 2)
            self.assertEqual(len(list(Path(directory).glob("*.json"))), 2)

    def test_daily_cache_and_targeted_rerun_do_not_rediscover(self):
        with TemporaryDirectory() as directory, \
                patch.object(discovery, "discovery_page") as browser, \
                patch.object(discovery, "discover_competition", return_value=[fixture()]) as discover:
            session = MagicMock()
            result = discovery.get_daily_matches(["l1"], session, today=DAY, cache_dir=directory)
            self.assertEqual(result, [fixture()])
            again = discovery.get_daily_matches(
                ["l1"], session, queries=["PSG"], today=DAY, cache_dir=directory,
            )
            self.assertEqual(again, result)
            self.assertEqual(discover.call_count, 1)
            self.assertEqual(browser.call_count, 1)
            discovery.get_daily_matches(["l1"], session, refresh=True, today=DAY, cache_dir=directory)
            self.assertEqual(discover.call_count, 2)

    def test_competition_filter_applies_before_discovery_and_empty_days_are_cached(self):
        with TemporaryDirectory() as directory, patch.object(discovery, "discovery_page"), \
                patch.object(discovery, "discover_competition", return_value=[]) as discover:
            self.assertEqual(discovery.get_daily_matches(
                ["pl"], MagicMock(), today=DAY, cache_dir=directory), [])
            self.assertEqual(discover.call_args.args[2], "pl")
            self.assertEqual(discover.call_count, 1)
            payload = json.loads((Path(directory) / f"{DAY}.json").read_text())
            self.assertEqual(payload["competitions"], {"pl": []})

    def test_stale_day_is_not_reused_and_corrupt_cache_is_not_silently_ignored(self):
        with TemporaryDirectory() as directory, patch.object(discovery, "discovery_page"), \
                patch.object(discovery, "discover_competition", return_value=[]):
            path = Path(directory) / f"{DAY}.json"
            path.write_text('{"version": 1, "date": "2000-01-01", "competitions": {}}')
            with self.assertRaisesRegex(ValueError, "Cache incompatible"):
                discovery.get_daily_matches(["l1"], MagicMock(), today=DAY, cache_dir=directory)
            discovery.get_daily_matches(["l1"], MagicMock(), refresh=True, today=DAY, cache_dir=directory)


class ManualAndPipelineTests(unittest.TestCase):
    def test_manual_csv_uses_headers_and_preserves_blank_betclic(self):
        args = parser.parse_args(["--file", "manual.csv"])
        frame = pd.DataFrame([{"url_betclic": "", "event_wina": 456, "event_pmu": 123}])
        with patch.object(parser.pd, "read_csv", return_value=frame):
            result = parser.load_manual_matches(args)
        self.assertEqual(result[0]["event_pmu"], 123)
        self.assertEqual(result[0]["url_betclic"], "")
        args.competition = ["l1"]
        with patch.object(parser.pd, "read_csv", return_value=frame), self.assertRaisesRegex(
            ValueError, "colonne competition",
        ):
            parser.load_manual_matches(args)

    def test_official_lineup_is_used_without_flag_and_absence_keeps_all_players(self):
        frame = pd.DataFrame([
            {"Match": "PSG - Marseille", "Equipe": team, "home": home,
             "Taille Combi": 1, "Joueurs": name, "Cote PMU": 2.0}
            for home, team, name in ((1, "PSG", "Alice"), (0, "Marseille", "Bob"))
        ])
        for lineups in (None, {1: [], 0: []}):
            with self.subTest(lineups=lineups), \
                    patch.object(parser, "get_pmu_players_dict", return_value=frame), \
                    patch.object(parser, "get_match_lineups", return_value=lineups) as get_lineups, \
                    patch.object(parser, "filter_players", return_value=frame) as filter_players, \
                    patch.object(parser, "run_master_wina_and_pmu", return_value=(frame, pd.DataFrame())), \
                    patch.object(parser, "export_to_google_sheets"):
                parser.process_single_match(123, 456, "", parser.parse_args([]), "key", 0,
                                            flashscore_url="https://fixture.test/match")
                self.assertFalse(get_lineups.call_args.kwargs["allow_probable"])
                self.assertEqual(filter_players.call_count, 0 if lineups is None else 1)

    def test_main_routes_discovered_records_without_running_other_matches(self):
        with patch.object(parser, "configure_logging"), \
                patch.object(parser, "get_daily_matches", return_value=[fixture()]) as discover, \
                patch.object(parser, "process_single_match") as process:
            parser.main(["-c", "l1", "-m", "PSG", "--date", "2026-10-15"])
        self.assertEqual(discover.call_args.args[0], ["l1"])
        self.assertEqual(discover.call_args.kwargs["queries"], ["PSG"])
        self.assertEqual(discover.call_args.kwargs["today"], DAY)
        self.assertEqual(process.call_count, 1)
        self.assertEqual(process.call_args.args[-1], "https://fixture.test/match")


if __name__ == "__main__":
    unittest.main()
