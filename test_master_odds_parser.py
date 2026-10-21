from datetime import datetime, timedelta
from itertools import combinations
import argparse
import unittest
from unittest.mock import MagicMock, call, patch

import pandas as pd
import requests

from flashscore_lineups import Lineups
import master_odds_parser as parser


def players_frame():
    return pd.DataFrame([
        {"Match": "Home - Away", "Equipe": "Home", "home": 1, "Taille Combi": 1,
         "Joueurs": "Alice", "outcome_id": "1", "Cote PMU": 2.0},
        {"Match": "Home - Away", "Equipe": "Home", "home": 1, "Taille Combi": 1,
         "Joueurs": "Bob", "outcome_id": "2", "Cote PMU": 3.0},
        {"Match": "Home - Away", "Equipe": "Home", "home": 1, "Taille Combi": 1,
         "Joueurs": "Chris", "outcome_id": "3", "Cote PMU": 5.0},
        {"Match": "Home - Away", "Equipe": "Away", "home": 0, "Taille Combi": 1,
         "Joueurs": "Dan", "outcome_id": "4", "Cote PMU": 2.0},
        {"Match": "Home - Away", "Equipe": "Away", "home": 0, "Taille Combi": 1,
         "Joueurs": "Eve", "outcome_id": "5", "Cote PMU": 3.0},
    ], index=["a", "b", "c", "d", "e"])


def wina_market(players, quotes=()):
    """Faux WinamaxMyMatch : simples lus sur le socket puis cotes MyMatch."""
    market = MagicMock()
    market.read_players.return_value = players
    market.quote.side_effect = list(quotes)
    return market


class CombinationTests(unittest.TestCase):
    def test_preserves_filter_order_and_pmu_only_players(self):
        frame = players_frame()
        combos = list(parser.iter_pmu_combinations(
            frame, {"Chris": 1.15}, [2, 3], 4.0, 1.15,
        ))
        names = [tuple(p["Joueurs"] for p in combo) for combo in combos]
        expected = list(combinations(["Alice", "Bob", "Chris"], 2))
        expected += [("Alice", "Bob", "Chris"), ("Dan", "Eve")]
        self.assertEqual(names, expected)

    def test_thresholds_and_empty_pool(self):
        frame = players_frame()
        self.assertEqual(list(parser.iter_pmu_combinations(frame, {}, [2, 3], 2.0, 1.15)), [])
        combos = list(parser.iter_pmu_combinations(frame, {"Chris": 1.149}, [2], 4.0, 1.15))
        self.assertEqual(len(combos), 2)

    def test_returns_every_eligible_combination(self):
        frame = players_frame()
        all_combos = list(parser.iter_pmu_combinations(frame, {}, [2, 3], 10, 1.15))
        self.assertEqual(len(all_combos), 5)
        self.assertEqual(tuple(p["Joueurs"] for p in all_combos[-1]), ("Dan", "Eve"))

    def test_generation_remains_lazy(self):
        frame = players_frame()
        original = combinations
        yielded = []

        def tracked(pool, size):
            for combo in original(pool, size):
                yielded.append(combo)
                yield combo

        with patch.object(parser, "combinations", side_effect=tracked):
            result = parser.iter_pmu_combinations(frame, {}, [2, 3], 10, 1.15)
            self.assertEqual(yielded, [])
            next(result)
        self.assertEqual(len(yielded), 1)

    def test_invalid_sizes(self):
        for sizes in ([1], [0]):
            with self.subTest(sizes=sizes), self.assertRaises(ValueError):
                parser.iter_pmu_combinations(players_frame(), {}, sizes, 4, 1.15)


class CliTests(unittest.TestCase):
    def test_flashscore_is_opt_in(self):
        self.assertFalse(parser.parse_args([]).flashscore)
        self.assertTrue(parser.parse_args(["--flashscore"]).flashscore)

    def test_default_input_is_automatic_discovery(self):
        args = parser.parse_args([])
        self.assertIsNone(args.file)
        for name in ("limit", "event_pmu", "event_wina", "url_betclic"):
            self.assertFalse(hasattr(args, name))

    def test_alternative_csv_and_other_options(self):
        args = parser.parse_args(["--file", "alternative.csv", "--tailles", "2", "--no-headless"])
        self.assertEqual(args.file, "alternative.csv")
        self.assertEqual(args.tailles, [2])
        self.assertFalse(args.headless)

    def test_removed_arguments_are_rejected(self):
        for flag in ("--limit", "-l", "--event-pmu", "-p", "--event-wina", "-w", "--url-betclic", "-b"):
            with self.subTest(flag=flag), patch("sys.stderr"), self.assertRaises(SystemExit) as error:
                parser.parse_args([flag, "123"])
            self.assertEqual(error.exception.code, 2)


class MatchingTests(unittest.TestCase):
    def test_unavailable_odds_warn_only_for_retained_pmu_players(self):
        wina = [
            {"home": 1, "Nom": "Alice", "Cote": 2.0},
            {"home": 1, "Nom": "Erwan Colas", "Cote": float("nan")},
        ]
        frame = players_frame().iloc[:1].copy()
        with self.assertNoLogs(level="WARNING"):
            mapping, _, _ = parser.match_wina_players(frame, wina, 80)
        self.assertEqual(mapping, {"Alice": "Alice"})
        frame.loc["extra"] = dict(frame.iloc[0], Joueurs="Erwan Colas")
        with self.assertLogs(level="WARNING") as logs:
            mapping, _, _ = parser.match_wina_players(frame, wina, 80)
        self.assertEqual(mapping, {"Alice": "Alice"})
        self.assertEqual(len(logs.output), 1)
        self.assertIn('PMU="Erwan Colas" ; WINAMAX="Erwan Colas"', logs.output[0])

    def test_matching_is_team_scoped_and_computes_ratios(self):
        wina = [
            {"home": 1, "Nom": "ALICE", "Cote": 1.5},
            {"home": 0, "Nom": "Bob", "Cote": 2.0},
        ]
        mapping, ratios, results = parser.match_wina_players(players_frame(), wina, 100)
        self.assertEqual(mapping, {"Alice": "ALICE"})
        self.assertAlmostEqual(ratios["Alice"], 2 / 1.5)
        self.assertEqual(results, [{"Taille Combi": 1, "Joueurs": "Alice", "Cote Wina": 1.5}])

    def test_exact_match_wins_over_fuzzy_collision(self):
        frame = players_frame().iloc[:2].copy()
        frame["Joueurs"] = ["Alicia", "Alice"]
        with self.assertLogs(level="INFO") as logs:
            mapping, _, _ = parser.match_wina_players(
                frame, [{"home": 1, "Nom": "Alice", "Cote": 2.0}], 50,
            )
        self.assertEqual(mapping, {"Alice": "Alice"})
        self.assertIn("tous les candidats sont deja attribues", " ".join(logs.output))

    def test_empty_candidates_and_invalid_odds(self):
        self.assertEqual(parser.match_wina_players(players_frame(), [], 85), ({}, {}, []))
        for odds in (0, -1, float("nan"), float("inf")):
            with self.subTest(odds=odds), self.assertLogs(level="WARNING"):
                result = parser.match_wina_players(
                    players_frame(), [{"home": 1, "Nom": "Alice", "Cote": odds}], 100,
                )
                self.assertEqual(result, ({}, {}, []))


class OddsReadinessTests(unittest.TestCase):
    def test_requires_every_selection_with_a_finite_price_above_one(self):
        frame = players_frame().drop(columns="outcome_id")
        parser.require_odds(frame, frame, "Cote PMU")
        for value in (None, float("nan"), float("inf"), 0, 1, -1):
            with self.subTest(value=value):
                quotes = frame.copy()
                quotes.loc["a", "Cote PMU"] = value
                with self.assertRaisesRegex(parser.OddsNotReady, "Alice"):
                    parser.require_odds(frame, quotes, "Cote PMU")
        for quotes in (pd.DataFrame(), frame.iloc[1:]):
            with self.assertRaisesRegex(parser.OddsNotReady, "Alice"):
                parser.require_odds(frame, quotes, "Cote PMU")

    def test_wina_missing_single_defers_before_combination_requests(self):
        with patch.object(parser, "wina_page"), \
                patch.object(parser, "WinamaxMyMatch", return_value=wina_market([])), \
                patch.object(parser, "get_pmu_combi_odds") as quote, \
                self.assertRaises(parser.OddsNotReady):
            parser.run_master_wina_and_pmu(
                "url", 123, players_frame(), [2], 4, 1.15, require_complete=True,
            )
        quote.assert_not_called()

    def test_missing_wina_market_defers_or_keeps_pmu_only(self):
        with patch.object(parser, "wina_page"), \
                patch.object(parser, "WinamaxMyMatch", return_value=wina_market(None)), \
                patch.object(parser, "get_pmu_combi_odds") as quote:
            with self.assertRaisesRegex(parser.OddsNotReady, "Joueur decisif"):
                parser.run_master_wina_and_pmu(
                    "url", 123, players_frame(), [2], 4, 1.15, require_complete=True,
                )
            with self.assertLogs(level="WARNING"):
                pmu, wina = parser.run_master_wina_and_pmu("url", 123, players_frame(), [2], 4, 1.15)
        self.assertEqual(pmu["Joueurs"].tolist(), players_frame()["Joueurs"].tolist())
        self.assertTrue(wina.empty)
        quote.assert_not_called()

    def test_kickoff_during_processing_is_ignored_not_deferred(self):
        start = datetime.now(parser.PARIS) + timedelta(minutes=20)
        clock = [start - timedelta(minutes=20)]

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        def market_closes_at_kickoff(*args, **kwargs):
            clock[0] = start + timedelta(seconds=1)
            raise parser.OddsNotReady("Marche Joueur décisif ferme sur Winamax")

        with patch.object(parser, "datetime", Clock), \
                patch.object(parser, "get_pmu_players_dict", return_value=players_frame()), \
                patch.object(parser, "get_match_lineups", return_value=Lineups("officielles")), \
                patch.object(parser, "filter_players", side_effect=lambda frame, _: frame), \
                patch.object(parser, "run_master_wina_and_pmu", side_effect=market_closes_at_kickoff), \
                patch.object(parser, "export_to_google_sheets") as export, \
                self.assertLogs(level="INFO") as logs:
            result = parser.process_single_match(
                123, 456, "", parser.parse_args(["--scheduled"]), "key.json", 0,
                flashscore_url="https://fixture.test", match_start=start.isoformat(),
            )
        self.assertEqual(result, "ignored")
        export.assert_not_called()
        self.assertIn("coup d'envoi atteint pendant le traitement", " ".join(logs.output))

    def run_scheduled_match(self, missing=None, minutes_before=24 * 60, lineups_seen=True,
                            kind="officielles", probable=False):
        singles = pd.DataFrame([
            dict(players_frame().iloc[0], home=home, Joueurs=f"Player {home} {i}",
                 outcome_id=str(home * 10 + i))
            for home in (1, 0) for i in range(10)
        ])
        final = singles.drop(columns="outcome_id")
        for size in (2, 3):
            final.loc[len(final)] = dict(final.iloc[0], **{
                "Taille Combi": size, "Joueurs": f"Combo {size}",
            })
        wina = final[["Taille Combi", "Joueurs"]].assign(**{"Cote Wina": 2.0})
        quotes = final[final["Taille Combi"] <= 2][["Taille Combi", "Joueurs"]].assign(
            **{"Cote Betclic": 2.0},
        )
        if missing == "lineup":
            singles = singles.iloc[1:]
        elif missing == "pmu_single":
            singles.loc[0, "Cote PMU"] = None
        elif missing == "betclic_single":
            quotes = quotes.iloc[1:]
        elif missing == "betclic_duo":
            quotes = quotes[quotes["Taille Combi"] == 1]
        elif missing == "wina_combo":
            wina = wina[wina["Taille Combi"] == 1]
        elif missing == "wina_market":
            wina = pd.DataFrame()
        start = datetime.now(parser.PARIS) + timedelta(minutes=minutes_before)
        args = parser.parse_args(["--scheduled"])
        with patch.object(parser, "get_pmu_players_dict", return_value=singles), \
                patch.object(parser, "get_match_lineups",
                             return_value=kind and Lineups(kind)) as get_lineups, \
                patch.object(parser, "filter_players", side_effect=lambda frame, _: frame), \
                patch.object(parser, "run_betclic_extraction", return_value=quotes), \
                patch.object(parser, "run_master_wina_and_pmu",
                             return_value=(final, wina)) as pipeline, \
                patch.object(parser, "export_to_google_sheets") as export, \
                patch.object(parser.time, "sleep"):
            self.pipeline, self.get_lineups = pipeline, get_lineups
            result = parser.process_single_match(
                123, 456, "https://betclic.test", args, "key.json", 0,
                flashscore_url="https://fixture.test", match_start=start.isoformat(),
                lineups_seen=lineups_seen, **({"probable": True} if probable else {}),
            )
        return result, pipeline, export

    def test_official_export_is_labelled_and_never_accepts_probables(self):
        result, _, export = self.run_scheduled_match()
        self.assertEqual(result, "exported")
        self.assertFalse(self.get_lineups.call_args.kwargs["allow_probable"])
        self.assertEqual(set(export.call_args.args[0]["Compositions"]), {"officielles"})

    def test_probable_export_neither_waits_for_stability_nor_complete_singles(self):
        with self.assertNoLogs(level="WARNING"):
            result, pipeline, export = self.run_scheduled_match(
                "pmu_single", lineups_seen=False, kind="probables", probable=True,
            )
        self.assertEqual(result, "exported")
        self.assertTrue(self.get_lineups.call_args.kwargs["allow_probable"])
        self.assertNotIn("require_complete", pipeline.call_args.kwargs)
        self.assertEqual(set(export.call_args.args[0]["Compositions"]), {"probables"})

    def test_probable_export_waits_for_probables_and_for_the_winamax_market(self):
        for kind in (None, "officielles"):
            with self.subTest(kind=kind), self.assertLogs(level="INFO") as logs:
                result, pipeline, export = self.run_scheduled_match(kind=kind, probable=True)
                self.assertEqual(result, "ignored")
                pipeline.assert_not_called()
                export.assert_not_called()
            self.assertIn("non publiees" if kind is None else "officielles deja publiees",
                          " ".join(logs.output))
        with self.assertRaisesRegex(parser.OddsNotReady, "pas encore ouvert"), \
                self.assertLogs(level="INFO"):
            self.run_scheduled_match("wina_market", kind="probables", probable=True)

    def test_missing_pmu_single_defers_before_pipeline(self):
        with self.assertRaisesRegex(parser.OddsNotReady, "Player 1 0"), \
                self.assertLogs(level="INFO"):
            self.run_scheduled_match("pmu_single")

    def test_only_pmu_and_wina_singles_are_required(self):
        for missing in (None, "lineup", "betclic_single", "betclic_duo", "wina_combo"):
            with self.subTest(missing=missing):
                result, pipeline, export = self.run_scheduled_match(missing)
                self.assertEqual(result, "exported")
                self.assertTrue(pipeline.call_args.kwargs["require_complete"])
                export.assert_called_once()

    def test_first_lineup_sighting_defers_before_pipeline(self):
        with self.assertRaises(parser.LineupsJustReleased), self.assertLogs(level="INFO"):
            self.run_scheduled_match(lineups_seen=False)
        self.pipeline.assert_not_called()

    def test_last_chance_does_not_wait_after_first_lineup_sighting(self):
        result, _, export = self.run_scheduled_match(minutes_before=20, lineups_seen=False)
        self.assertEqual(result, "exported")
        export.assert_called_once()

    def test_last_chance_exports_incomplete_singles(self):
        for minutes in (30, 10):
            with self.subTest(minutes=minutes):
                result, pipeline, export = self.run_scheduled_match("pmu_single", minutes)
                self.assertEqual(result, "exported")
                self.assertNotIn("require_complete", pipeline.call_args.kwargs)
                export.assert_called_once()


class PmuTests(unittest.TestCase):
    def test_quote_warning_uses_match_name_when_available(self):
        session = MagicMock()
        session.get.return_value.json.return_value = {}
        with self.assertLogs(level="WARNING") as logs:
            parser.get_pmu_combi_odds(123, ["1", "2"], session, match_name="Home - Away")
        self.assertIn("[Home - Away]", logs.output[0])
        self.assertNotIn("[123]", logs.output[0])

    def test_session_retries_are_bounded_and_get_only(self):
        with parser.create_pmu_session() as session:
            retry = session.get_adapter("https://example.test").max_retries
            self.assertEqual(retry.total, 2)
            self.assertEqual(retry.allowed_methods, frozenset({"GET"}))
            self.assertTrue(retry.respect_retry_after_header)
            self.assertIn(429, retry.status_forcelist)

    def test_quote_reuses_session_and_preserves_scaling(self):
        session = MagicMock()
        for raw, expected in ((4500, 4.5), (4.5, 4.5)):
            session.get.return_value.json.return_value = {"selectedOdds": {"decimal": raw}}
            self.assertEqual(parser.get_pmu_combi_odds(123, ["1", "2"], session), expected)
            self.assertEqual(session.get.call_args.kwargs["timeout"], parser.PMU_TIMEOUT)
            self.assertIn("/outcome/1,2.json", session.get.call_args.args[0])
        self.assertEqual(session.get.return_value.raise_for_status.call_count, 2)

    def test_failed_or_unavailable_quotes_are_logged(self):
        session = MagicMock()
        for payload in ({}, {"selectedOdds": {"decimal": 0}},
                        {"selectedOdds": {"decimal": "invalid"}},
                        {"selectedOdds": {"decimal": float("nan")}}):
            session.get.return_value.json.return_value = payload
            with self.subTest(payload=payload), self.assertLogs(level="WARNING"):
                self.assertIsNone(parser.get_pmu_combi_odds(123, ["1", "2"], session))
        for error in (requests.Timeout("timeout"), requests.HTTPError("503")):
            session.get.side_effect = error
            with self.subTest(error=error), self.assertLogs(level="WARNING"):
                self.assertIsNone(parser.get_pmu_combi_odds(123, ["1", "2"], session))

    def test_missing_event_is_logged_and_returns_empty(self):
        session = MagicMock()
        session.get.return_value.json.return_value = {"events": [], "betOffers": []}
        with self.assertLogs(level="WARNING"):
            self.assertTrue(parser.get_pmu_players_dict(123, "market", session).empty)
        self.assertEqual(session.get.call_args.kwargs["timeout"], parser.PMU_TIMEOUT)


class WinamaxQuoteTests(unittest.TestCase):
    def test_same_combination_is_retried_after_reconnection(self):
        market = wina_market([], [parser.WinamaxSocketError("socket ferme"), 6.0])
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(parser.quote_wina_combination(market, [1, 2], "Alice - Bob", "M"), 6.0)
        self.assertEqual(market.quote.call_args_list, [call([1, 2]), call([1, 2])])
        market.reconnect.assert_called_once()
        self.assertIn("Echec tentative 1/2 pour Alice - Bob", logs.output[0])

    def test_persistent_failure_stops_instead_of_cascading(self):
        second = ValueError("Reponse MyMatch incoherente")
        market = wina_market([], [parser.WinamaxSocketError("aucune reponse"), second])
        with self.assertLogs(level="WARNING"), \
                self.assertRaisesRegex(RuntimeError, "Alice - Bob; arret du match") as error:
            parser.quote_wina_combination(market, [1, 2], "Alice - Bob", "M")
        self.assertIs(error.exception.__cause__, second)
        self.assertEqual(market.quote.call_count, 2)

    def test_failed_recovery_keeps_combination_context_and_original_error(self):
        failure = parser.WinamaxSocketError("Connexion socket Winamax impossible")
        market = wina_market([], [parser.WinamaxSocketError("socket ferme")])
        market.reconnect.side_effect = failure
        with self.assertLogs(level="WARNING"), \
                self.assertRaisesRegex(RuntimeError, "Alice - Bob.*sans export") as error:
            parser.quote_wina_combination(market, [1, 2], "Alice - Bob", "M")
        self.assertIs(error.exception.__cause__, failure)

    def test_refused_combination_is_skipped_without_reconnection(self):
        market = wina_market([], [parser.MyMatchRefused("MyMatch refuse par Winamax : -7")])
        with self.assertLogs(level="WARNING") as logs:
            self.assertIsNone(parser.quote_wina_combination(market, [1, 2], "Alice - Bob", "M"))
        market.reconnect.assert_not_called()
        self.assertIn("[M] Alice - Bob : MyMatch refuse", logs.output[0])

    def test_closed_market_is_not_ready_rather_than_an_error(self):
        closed = parser.WinamaxMarketClosed("statut PREMATCH -> LIVE")
        market = wina_market([], [closed])
        with self.assertRaisesRegex(parser.OddsNotReady, "LIVE"):
            parser.quote_wina_combination(market, [1, 2], "Alice - Bob", "M")
        market = wina_market([], [parser.WinamaxSocketError("socket ferme")])
        market.reconnect.side_effect = closed
        with self.assertLogs(level="WARNING"), self.assertRaisesRegex(parser.OddsNotReady, "LIVE"):
            parser.quote_wina_combination(market, [1, 2], "Alice - Bob", "M")


class PipelineTests(unittest.TestCase):
    def test_browser_closes_on_failure(self):
        with patch.object(parser, "sync_playwright") as playwright:
            browser = playwright.return_value.__enter__.return_value.chromium.launch.return_value
            with self.assertRaisesRegex(RuntimeError, "test"):
                with parser.wina_page(True):
                    raise RuntimeError("test")
            browser.close.assert_called_once()

    def test_pipeline_keeps_unmatched_pmu_combos_and_passes_session(self):
        session = MagicMock()
        with patch.object(parser, "wina_page") as browser, \
                patch.object(parser, "WinamaxMyMatch", return_value=wina_market([])) as market, \
                patch.object(parser, "get_pmu_combi_odds", return_value=6.5) as quote, \
                patch.object(parser.time, "sleep"):
            pmu, wina = parser.run_master_wina_and_pmu(
                "https://example.test", 123, players_frame(), [2, 3],
                4, 1.15, pmu_session=session,
            )
        page = browser.return_value.__enter__.return_value
        market.assert_called_once_with(page, "https://example.test")
        market.return_value.open.assert_called_once()
        market.return_value.quote.assert_not_called()
        self.assertEqual(len(pmu), 7)
        self.assertEqual(pmu.iloc[-1]["Joueurs"], "Dan - Eve")
        self.assertEqual(pmu.iloc[-1]["Cote PMU"], 6.5)
        self.assertNotIn("outcome_id", pmu.columns)
        self.assertTrue(wina.empty)
        self.assertEqual(quote.call_count, 2)
        quote.assert_any_call(123, ["1", "2"], session=session, match_name="Home - Away")
        quote.assert_any_call(123, ["4", "5"], session=session, match_name="Home - Away")
        session.close.assert_not_called()

    def test_combinations_use_winamax_outcome_ids_of_matched_players(self):
        players = [
            {"home": 1, "Nom": "ALICE", "Cote": 1.5, "outcome_id": 11},
            {"home": 1, "Nom": "Bob", "Cote": 2.0, "outcome_id": 12},
            {"home": 1, "Nom": "Chris", "Cote": float("nan"), "outcome_id": 13},
        ]
        market = wina_market(players, [6.0])
        with patch.object(parser, "wina_page"), \
                patch.object(parser, "WinamaxMyMatch", return_value=market), \
                patch.object(parser, "get_pmu_combi_odds", return_value=7.0), \
                patch.object(parser.time, "sleep"), self.assertLogs(level="WARNING"):
            pmu, wina = parser.run_master_wina_and_pmu(
                "url", 123, players_frame().iloc[:3], [2, 3], 10, 1.15, name_match_score=100,
            )
        # Chris n'a pas de cote Wina : ses combinaisons restent PMU seules.
        market.quote.assert_called_once_with([11, 12])
        self.assertEqual(pmu["Joueurs"].tolist()[3:],
                         ["Alice - Bob", "Alice - Chris", "Bob - Chris", "Alice - Bob - Chris"])
        self.assertEqual(wina.to_dict("records"), [
            {"Taille Combi": 1, "Joueurs": "Alice", "Cote Wina": 1.5},
            {"Taille Combi": 1, "Joueurs": "Bob", "Cote Wina": 2.0},
            {"Taille Combi": 2, "Joueurs": "Alice - Bob", "Cote Wina": 6.0},
        ])


class SheetsTests(unittest.TestCase):
    def setUp(self):
        self.frame = pd.DataFrame({
            "Match": ["Home - Away", "Home - Away"],
            "Taille Combi": [1, 2], "Joueurs": ["Alice", "Alice - Bob"],
            "Cote PMU": [2.0, 6.0], "Cote Wina": [1.5, 5.0],
            "Ratio (PMU/Wina)": [2 / 1.5, 1.2],
        })
        self.auth_patch = patch.object(parser.gspread, "service_account")
        self.auth = self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.sheet = self.auth.return_value.open.return_value
        self.worksheet = self.sheet.worksheet.return_value
        self.worksheet.id = 42
        self.worksheet.row_values.return_value = ["Heure", *self.frame.columns.tolist()]

    def test_successful_new_sheet_with_kickoff_is_registered_for_cleanup(self):
        self.sheet.worksheet.side_effect = parser.gspread.exceptions.WorksheetNotFound("missing")
        worksheet = self.sheet.add_worksheet.return_value
        worksheet.id = 43
        worksheet.col_values.return_value = []
        start = "2026-10-19T21:00:00+02:00"
        with patch.object(parser, "set_with_dataframe"), \
                patch.object(parser, "track_match_sheet") as track:
            parser.export_to_google_sheets(self.frame, "key.json", "book", "match",
                                           match_start=start)
        track.assert_called_once_with(self.sheet.id, 43, "match", start, True)

    def test_failed_export_is_not_registered_for_cleanup(self):
        self.worksheet.col_values.return_value = []
        self.sheet.batch_update.side_effect = RuntimeError("quota")
        with patch.object(parser, "set_with_dataframe"), \
                patch.object(parser, "track_match_sheet") as track, \
                self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "quota"):
            parser.export_to_google_sheets(self.frame, "key.json", "book", "match",
                                           match_start="2026-10-19T21:00:00+02:00")
        track.assert_not_called()

    def test_betclic_and_winamax_have_identical_gradients_per_combination_size(self):
        for betclic_only in (False, True):
            frame = self.frame.copy()
            frame["Ratio (PMU/Betclic)"] = [1.1, float("nan")]
            if betclic_only:
                frame = frame.drop(columns="Ratio (PMU/Wina)")
            for existing in ([], ["Heure", "old"]):
                with self.subTest(betclic_only=betclic_only, existing=existing), \
                        patch.object(parser, "set_with_dataframe"):
                    self.worksheet.col_values.return_value = existing
                    self.worksheet.row_values.return_value = ["Heure", *frame.columns.tolist()]
                    parser.export_to_google_sheets(frame, "key.json", "book", "match")
                    requests = self.sheet.batch_update.call_args.args[0]["requests"]
                    rules = [r["addConditionalFormatRule"]["rule"] for r in requests
                             if "addConditionalFormatRule" in r]
                    expected_columns = [
                        frame.columns.get_loc(name) + 1
                        for name in ("Ratio (PMU/Wina)", "Ratio (PMU/Betclic)")
                        if name in frame.columns
                    ]
                    first_data = 2 if existing else 1
                    self.assertEqual(
                        [(r["ranges"][0]["startRowIndex"], r["ranges"][0]["endRowIndex"],
                          r["ranges"][0]["startColumnIndex"]) for r in rules],
                        [(row, row + 1, column)
                         for row in (first_data, first_data + 1) for column in expected_columns],
                    )
                    self.assertTrue(all(r["gradientRule"] == rules[0]["gradientRule"] for r in rules))
                    self.assertEqual(rules[0]["gradientRule"]["midpoint"]["value"], "50")

    def test_empty_and_existing_sheet_ranges_and_batched_formatting(self):
        for existing, start, header, end in (
            ([], 1, True, 3),
            (["Heure", "old", "", "old"], 5, False, 6),
        ):
            with self.subTest(existing=existing), patch.object(parser, "set_with_dataframe") as write:
                self.worksheet.col_values.return_value = existing
                parser.export_to_google_sheets(self.frame, "key.json", "book", "match")
                self.assertEqual(write.call_args.kwargs, {
                    "row": start, "include_column_header": header,
                })
                exported = write.call_args.args[1]
                self.assertEqual(exported.columns[0], "Heure")
                self.assertEqual(len(exported), len(self.frame))
                requests = self.sheet.batch_update.call_args.args[0]["requests"]
                filters = [r["setBasicFilter"] for r in requests if "setBasicFilter" in r]
                self.assertEqual(filters[0]["filter"]["range"]["endRowIndex"], end)
                formats = [r["repeatCell"] for r in requests if "repeatCell" in r]
                self.assertEqual(len(formats), 3)
                self.assertTrue(all(r["range"]["endRowIndex"] == end for r in formats))
                gradients = [r["addConditionalFormatRule"]["rule"]["ranges"][0]
                             for r in requests if "addConditionalFormatRule" in r]
                first_data = start if header else start - 1
                self.assertEqual([r["startRowIndex"] for r in gradients], [first_data, first_data + 1])
                self.assertEqual([r["endRowIndex"] for r in gradients], [first_data + 1, first_data + 2])
        self.assertEqual(self.sheet.batch_update.call_count, 2)
        self.worksheet.get_all_values.assert_not_called()
        self.worksheet.format.assert_not_called()
        self.assertNotIn("Heure", self.frame.columns)

    def test_existing_sheets_gain_added_columns_left_to_right(self):
        frame = self.frame.copy()
        frame.insert(0, "Compositions", "probables")
        frame.insert(frame.columns.get_loc("Cote Wina") + 1, "Cote Betclic", 2.5)
        expected = ["Heure", *frame.columns]
        for missing in (["Compositions"], ["Compositions", "Cote Betclic"]):
            with self.subTest(missing=missing), patch.object(parser, "set_with_dataframe"):
                self.worksheet.reset_mock()
                self.worksheet.col_values.return_value = ["Heure", "old"]
                self.worksheet.row_values.return_value = [c for c in expected if c not in missing]
                parser.export_to_google_sheets(frame, "key.json", "book", "match")
                self.assertEqual(self.worksheet.insert_cols.call_args_list,
                                 [call([[name]], col=expected.index(name) + 1) for name in missing])
        self.worksheet.row_values.return_value = ["Heure", "Autre", *frame.columns]
        with patch.object(parser, "set_with_dataframe"), self.assertLogs(level="ERROR"), \
                self.assertRaisesRegex(ValueError, "incompatibles"):
            parser.export_to_google_sheets(frame, "key.json", "book", "match")

    def test_export_failure_propagates(self):
        self.worksheet.col_values.return_value = []
        self.sheet.batch_update.side_effect = RuntimeError("quota")
        with patch.object(parser, "set_with_dataframe"), self.assertLogs(level="ERROR"), \
                self.assertRaisesRegex(RuntimeError, "quota"):
            parser.export_to_google_sheets(self.frame, "key.json", "book", "match")

    def test_match_failure_is_not_reported_as_success(self):
        args = argparse.Namespace(
            tailles=[2], cote_out=4, ratio_min=1.15,
            name_match_score=85, headless=True, flashscore=False, official_lineups_only=False,
        )
        singles = players_frame()
        with patch.object(parser, "GSHEET_NAME", "book", create=True), \
                patch.object(parser, "get_pmu_players_dict", return_value=singles), \
                patch.object(parser, "run_master_wina_and_pmu",
                             return_value=(singles.drop(columns=["outcome_id"]), pd.DataFrame())), \
                patch.object(parser, "export_to_google_sheets", side_effect=RuntimeError("export")), \
                self.assertLogs(level="INFO") as logs, self.assertRaisesRegex(RuntimeError, "export"):
            parser.process_single_match(123, 456, "", args, "key.json", 0)
        self.assertNotIn("Terminé avec succès", " ".join(logs.output))

    def test_final_export_preserves_columns_ratios_and_sort_order(self):
        args = argparse.Namespace(
            tailles=[2], cote_out=4, ratio_min=1.15,
            name_match_score=85, headless=True, flashscore=False, official_lineups_only=False,
        )
        singles = players_frame()
        wina = pd.DataFrame([
            {"Taille Combi": 1, "Joueurs": "Alice", "Cote Wina": 2.0},
            {"Taille Combi": 1, "Joueurs": "Bob", "Cote Wina": 2.0},
        ])
        with patch.object(parser, "GSHEET_NAME", "book", create=True), \
                patch.object(parser, "get_pmu_players_dict", return_value=singles), \
                patch.object(parser, "run_master_wina_and_pmu",
                             return_value=(singles.drop(columns=["outcome_id"]), wina)), \
                patch.object(parser, "export_to_google_sheets") as export:
            parser.process_single_match(123, 456, "", args, "key.json", 0)
        frame, credentials, book, match = export.call_args.args
        self.assertEqual(frame.columns.tolist(), [
            "Compositions", "Match", "Equipe", "Taille Combi", "Joueurs", "Cote PMU", "Cote Wina", "Cote Betclic",
            "Ratio (PMU/Wina)", "Ratio (PMU/Betclic)",
        ])
        self.assertEqual(set(frame["Compositions"]), {"aucune"})
        self.assertEqual(frame["Joueurs"].tolist(), ["Bob", "Alice", "Chris", "Dan", "Eve"])
        self.assertEqual(frame["Ratio (PMU/Wina)"].iloc[:2].tolist(), [1.5, 1.0])
        self.assertTrue(frame["Cote Wina"].iloc[2:].isna().all())
        self.assertEqual((credentials, book, match), ("key.json", "book", "Home - Away"))

    def test_flashscore_flag_controls_probable_policy_not_official_filtering(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                args = parser.parse_args(["--flashscore"] if enabled else [])
                expected = ["Alice", "Eve"]

                def run_wina(url, event, singles, *args, **kwargs):
                    self.assertEqual(singles["Joueurs"].tolist(), expected)
                    return singles.drop(columns=["outcome_id"]), pd.DataFrame()

                with patch.object(parser, "GSHEET_NAME", "book", create=True), \
                        patch.object(parser, "get_pmu_players_dict", return_value=players_frame()), \
                        patch.object(parser, "run_master_wina_and_pmu", side_effect=run_wina) as run, \
                        patch.object(parser, "get_match_lineups", return_value=Lineups("probables")) as get_lineups, \
                        patch.object(parser, "filter_players",
                                     return_value=players_frame().iloc[[0, 4]]) as filter_lineups, \
                        patch.object(parser, "export_to_google_sheets"):
                    parser.process_single_match(123, 456, "", args, "key.json", 0, ["match"])
                run.assert_called_once()
                self.assertEqual(get_lineups.call_count, 1)
                self.assertEqual(get_lineups.call_args.kwargs["allow_probable"], enabled)
                self.assertEqual(filter_lineups.call_count, 1)


if __name__ == "__main__":
    unittest.main()
