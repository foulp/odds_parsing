from itertools import combinations
import argparse
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
import requests

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


class BrowserTests(unittest.TestCase):
    def test_expansion_failure_is_retried_and_never_silently_accepted(self):
        tableau = MagicMock()
        button = tableau.locator.return_value.get_by_text.return_value.first
        button.count.return_value = 1
        button.is_visible.return_value = True
        button.click.side_effect = parser.PlaywrightTimeoutError("intercepted")
        with self.assertLogs(level="WARNING"), self.assertRaisesRegex(RuntimeError, "non entierement"):
            parser.expand_wina_team(tableau, 1)
        self.assertEqual(button.click.call_count, 3)
        button.click.assert_called_with(timeout=10000)

    def test_recovery_rebuilds_market_and_verifies_empty_basket(self):
        page, tableaux, button = MagicMock(), MagicMock(), MagicMock()
        with patch.object(parser, "prepare_wina_market", return_value=tableaux) as prepare, \
                patch.object(parser, "extract_wina_players",
                             return_value=({"Alice": button}, [])) as extract:
            result = parser.recover_wina_market(page, ["Alice"])
        page.reload.assert_called_once()
        prepare.assert_called_once_with(page)
        extract.assert_called_once_with(tableaux)
        button.wait_for.assert_called_once_with(state="visible", timeout=10000)
        basket = page.locator.return_value
        basket.get_by_text.return_value.wait_for.assert_called_once_with(
            state="visible", timeout=10000,
        )
        self.assertEqual(result, (tableaux, {"Alice": button}))
        page.evaluate.assert_not_called()

    def test_recovery_fails_explicitly_if_player_stays_missing(self):
        with patch.object(parser, "prepare_wina_market"), \
                patch.object(parser, "extract_wina_players", return_value=({}, [])), \
                self.assertRaisesRegex(ValueError, "Alice"):
            parser.recover_wina_market(MagicMock(), ["Alice"])

    def test_same_combination_is_retried_after_recovery(self):
        self.check_combination_recovery(persistent_failure=False)

    def test_persistent_failure_stops_instead_of_cascading(self):
        self.check_combination_recovery(persistent_failure=True)

    def check_combination_recovery(self, persistent_failure):
        frame = players_frame().iloc[:2]
        wina = [{"Nom": p["Joueurs"], "home": 1, "Cote": 2.0}
                for p in frame.to_dict("records")]
        initial, recovered = MagicMock(), MagicMock()
        failures = [parser.PlaywrightError("Element is not attached to the DOM"),
                    parser.PlaywrightTimeoutError("still missing") if persistent_failure else 6.0]
        with patch.object(parser, "wina_page"), \
                patch.object(parser, "prepare_wina_market", return_value=initial), \
                patch.object(parser, "extract_wina_players", return_value=({}, wina)), \
                patch.object(parser, "get_pmu_combi_odds", return_value=6.5), \
                patch.object(parser, "read_wina_combination", side_effect=failures) as read, \
                patch.object(parser, "recover_wina_market", return_value=(recovered, {})) as recover, \
                patch.object(parser.time, "sleep"), self.assertLogs(level="WARNING"):
            if persistent_failure:
                with self.assertRaisesRegex(RuntimeError, "arret du match"):
                    parser.run_master_wina_and_pmu("url", 123, frame, [2], 4, 1.15)
            else:
                _, result = parser.run_master_wina_and_pmu("url", 123, frame, [2], 4, 1.15)
                combo = result[result["Taille Combi"] == 2]
                self.assertEqual(combo["Cote Wina"].tolist(), [6.0])
                self.assertEqual(combo["Joueurs"].tolist(), ["Alice - Bob"])
        self.assertEqual(read.call_count, 2)
        self.assertIs(read.call_args_list[0].args[1], initial)
        self.assertIs(read.call_args_list[1].args[1], recovered)
        recover.assert_called_once()

    def test_quote_refreshes_buttons_and_waits_for_empty_basket(self):
        page = MagicMock()
        basket = page.locator.return_value
        items = basket.locator.return_value
        items.locator.return_value.first.inner_text.return_value = "6,5"
        alice, bob = MagicMock(), MagicMock()
        with patch.object(parser, "extract_wina_players",
                          return_value=({"Alice": alice, "Bob": bob}, [])) as extract, \
                patch.object(parser, "expect") as assertion, \
                patch.object(parser.time, "sleep"):
            self.assertEqual(parser.read_wina_combination(page, "tables", ["Alice", "Bob"]), 6.5)
        self.assertEqual(extract.call_count, 2)
        extract.assert_called_with("tables")
        alice.click.assert_called_once_with(force=True, timeout=10000)
        bob.click.assert_called_once_with(force=True, timeout=10000)
        self.assertEqual(basket.get_by_text.return_value.wait_for.call_count, 2)
        items.locator.assert_any_call(".odds-indicator-value > div")
        self.assertEqual(assertion.return_value.to_have_text.call_count, 5)

    def test_clear_uses_named_control_not_arbitrary_svg(self):
        page = MagicMock()
        basket = page.locator.return_value
        basket.get_by_text.return_value.is_visible.return_value = False
        parser.clear_wina_basket(page)
        basket.get_by_role.assert_called_once_with(
            "button", name="Effacer et continuer", exact=True,
        )
        basket.get_by_role.return_value.click.assert_called_once_with(
            timeout=10000,
        )
        basket.get_by_text.return_value.wait_for.assert_called_once_with(
            state="visible", timeout=10000,
        )

    def test_one_dom_read_per_team_and_original_button_indices(self):
        tableaux = MagicMock()
        tableaux.count.return_value = 2
        home, away = MagicMock(), MagicMock()
        tableaux.nth.side_effect = lambda i: SimpleNamespace(locator=lambda _: [home, away][i])
        home.evaluate.return_value = {
            "names": ["Alice", "Bob"], "odds": ["1", "2", "3,5", "1", "2", "4"], "buttons": 6,
        }
        away.evaluate.return_value = {
            "names": ["Dan"], "odds": ["1", "2", "2,5"], "buttons": 3,
        }
        buttons, players = parser.extract_wina_players(tableaux)
        self.assertEqual(home.evaluate.call_count + away.evaluate.call_count, 2)
        self.assertEqual([p["Cote"] for p in players], [3.5, 4.0, 2.5])
        self.assertEqual([p["home"] for p in players], [1, 1, 0])
        self.assertEqual(set(buttons), {"Alice", "Bob", "Dan"})
        self.assertEqual([c.args[0] for c in home.locator.return_value.nth.call_args_list], [2, 5])

    def test_changed_dom_shape_fails_explicitly(self):
        tableaux = MagicMock()
        tableaux.count.return_value = 1
        tableaux.nth.return_value.locator.return_value.evaluate.return_value = {
            "names": ["Alice"], "odds": ["1"], "buttons": 1,
        }
        with self.assertRaisesRegex(ValueError, "Structure Winamax"):
            parser.extract_wina_players(tableaux)

    def test_unavailable_odds_keep_identity_without_exposing_a_clickable_button(self):
        tableaux = MagicMock()
        tableaux.count.return_value = 1
        tableaux.nth.return_value.locator.return_value.evaluate.return_value = {
            "names": ["Erwan Colas"], "odds": ["1", "2", "-"], "buttons": 3,
        }
        with self.assertLogs(level="DEBUG") as logs:
            buttons, players = parser.extract_wina_players(tableaux)
        self.assertEqual(buttons, {})
        self.assertEqual(players[0]["Nom"], "Erwan Colas")
        self.assertTrue(pd.isna(players[0]["Cote"]))
        self.assertTrue(all(line.startswith("DEBUG:") for line in logs.output))

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
                patch.object(parser, "extract_wina_players", return_value=({}, [])), \
                patch.object(parser, "get_pmu_combi_odds", return_value=6.5) as quote, \
                patch.object(parser.time, "sleep"):
            page = browser.return_value.__enter__.return_value
            section = page.locator.return_value.filter.return_value.filter.return_value.last
            section.locator.return_value.count.return_value = 0
            pmu, wina = parser.run_master_wina_and_pmu(
                "https://example.test", 123, players_frame(), [2, 3],
                4, 1.15, pmu_session=session,
            )
        self.assertEqual(len(pmu), 7)
        self.assertEqual(pmu.iloc[-1]["Joueurs"], "Dan - Eve")
        self.assertEqual(pmu.iloc[-1]["Cote PMU"], 6.5)
        self.assertNotIn("outcome_id", pmu.columns)
        self.assertTrue(wina.empty)
        self.assertEqual(quote.call_count, 2)
        quote.assert_any_call(123, ["1", "2"], session=session, match_name="Home - Away")
        quote.assert_any_call(123, ["4", "5"], session=session, match_name="Home - Away")
        session.close.assert_not_called()


class LocalDomTests(unittest.TestCase):
    """Necessite Chromium Playwright ; aucune connexion aux bookmakers."""

    def test_expands_both_teams_including_offscreen_liverpool(self):
        with parser.wina_page(True) as page:
            page.set_content("""
                <div class="team">
                    <div class="bet-group-template">
                        <div><span class="label-line">LASK 1</span></div>
                    </div>
                    <button onclick="expand(this, 'LASK')">Plus de s\u00e9lections</button>
                </div>
                <div style="height:2500px"></div>
                <div class="team">
                    <div class="bet-group-template">
                        <div><span class="label-line">Barcola</span></div>
                        <div><span class="label-line">Koumas</span></div>
                    </div>
                    <button onclick="expand(this, 'Liverpool')">Plus de s\u00e9lections</button>
                </div>
                <script>
                    function expand(button, team) {
                        setTimeout(() => {
                            const group = button.parentElement.querySelector('.bet-group-template');
                            for (let i = 0; i < 10; i++) {
                                const row = document.createElement('div');
                                row.innerHTML = '<span class="label-line">' + team + i + '</span>';
                                group.appendChild(row);
                            }
                            button.remove();
                        }, 150);
                    }
                </script>
            """)
            groups = page.locator(".bet-group-template")
            parser.expand_wina_team(groups.nth(0), 0)
            parser.expand_wina_team(groups.nth(1), 1)
            self.assertEqual(groups.nth(0).locator(".label-line").count(), 11)
            self.assertEqual(groups.nth(1).locator(".label-line").count(), 12)
            self.assertEqual(page.get_by_text("Plus de s\u00e9lections", exact=True).count(), 0)

    def test_clear_accepts_empty_basket_even_when_odds_stay_visible(self):
        with parser.wina_page(True) as page:
            page.set_content("""
                <div data-testid="sticky-wrap">
                    <svg viewBox="0 0 20 20" onclick="throw Error('wrong control')"></svg>
                    <div role="button" aria-label="Effacer et continuer"
                         onclick="document.querySelector('#empty').hidden=false">Vider</div>
                    <div class="odds-indicator-value"><div>6,50</div></div>
                    <div id="empty" hidden>Ton panier est vide !</div>
                </div>
            """)
            parser.clear_wina_basket(page)
            self.assertTrue(page.locator(".odds-indicator-value").is_visible())
            self.assertTrue(page.get_by_text("Ton panier est vide !", exact=True).is_visible())
            parser.clear_wina_basket(page)

    def test_quote_reads_final_value_not_animation_history(self):
        with parser.wina_page(True) as page:
            page.set_content("""
                <button id="alice" onclick="add('Alice')">Alice</button>
                <button id="bob" onclick="add('Bob')">Bob</button>
                <div data-testid="sticky-wrap">
                  <div data-testid="basket-item-match" hidden>
                    <div class="odds-indicator-value">
                        <span><span>0,26</span><span>0,52</span><span>6,50</span></span>
                        <div>6,50</div>
                    </div>
                    <div id="players"></div>
                  </div>
                    <div role="button" aria-label="Effacer et continuer"
                         onclick="document.querySelector('#empty').hidden=false">Vider</div>
                    <div id="empty">Ton panier est vide !</div>
                </div>
                <script>
                  function add(name) {
                    document.querySelector('#empty').hidden=true;
                    document.querySelector('[data-testid="basket-item-match"]').hidden=false;
                    const row=document.createElement('div');
                    row.innerHTML='<span>Joueur décisif</span><span> '+name+'</span>';
                    document.querySelector('#players').append(row);
                  }
                </script>
            """)
            with patch.object(parser, "extract_wina_players", return_value=(
                {"Alice": page.locator("#alice"), "Bob": page.locator("#bob")}, [],
            )):
                self.assertEqual(parser.read_wina_combination(page, None, ["Alice", "Bob"]), 6.5)

    def test_quote_waits_for_delayed_duo_and_trio_instead_of_reading_single(self):
        for names in (["Alice", "Bob"], ["Alice", "Bob", "Chris"]):
            with self.subTest(names=names), parser.wina_page(True) as page:
                page.set_content("""
                    <button id="Alice" onclick="add('Alice')">Alice</button>
                    <button id="Bob" onclick="add('Bob')">Bob</button>
                    <button id="Chris" onclick="add('Chris')">Chris</button>
                    <div data-testid="sticky-wrap">
                      <div id="empty">Ton panier est vide !</div>
                      <div data-testid="basket-item-match" hidden>
                        <div id="players"></div>
                        <div class="odds-indicator-value"><div>3,20</div></div>
                      </div>
                      <div role="button" aria-label="Effacer et continuer"
                        onclick="document.querySelector('#empty').hidden=false">Vider</div>
                    </div>
                    <script>
                      let timer;
                      function add(name) {
                        clearTimeout(timer);
                        document.querySelector('#empty').hidden=true;
                        document.querySelector('[data-testid="basket-item-match"]').hidden=false;
                        setTimeout(() => {
                          const row=document.createElement('div');
                          row.innerHTML='<span>Joueur décisif</span><span> '+name+'</span>';
                          document.querySelector('#players').append(row);
                        }, 200);
                        timer=setTimeout(() => {
                          document.querySelector('.odds-indicator-value > div').textContent='9,00';
                        }, 1000);
                      }
                    </script>
                """)
                with patch.object(parser, "extract_wina_players", side_effect=lambda _: (
                    {name: page.locator(f"#{name}") for name in names}, [],
                )):
                    self.assertEqual(parser.read_wina_combination(page, None, names), 9.0)

    def test_quote_accepts_basket_padding_but_still_checks_exact_player_names(self):
        with parser.wina_page(True) as page:
            page.set_content("""
                <button id="enzo" onclick="add('Enzo&nbsp;Le Fée')">Enzo</button>
                <button id="nordi" onclick="add('Nordi Mukiele ')">Nordi</button>
                <div data-testid="sticky-wrap">
                  <div id="empty">Ton panier est vide !</div>
                  <div data-testid="basket-item-match" hidden>
                    <div id="players"></div>
                    <div class="odds-indicator-value"><div>9,00</div></div>
                  </div>
                  <div role="button" aria-label="Effacer et continuer"
                    onclick="document.querySelector('#empty').hidden=false">Vider</div>
                </div>
                <script>
                  function add(name) {
                    document.querySelector('#empty').hidden=true;
                    document.querySelector('[data-testid="basket-item-match"]').hidden=false;
                    const row=document.createElement('div');
                    row.innerHTML='<span>Joueur décisif</span><span>&nbsp;'+name+'</span>';
                    document.querySelector('#players').append(row);
                  }
                </script>
            """)
            with patch.object(parser, "extract_wina_players", return_value=(
                {"Enzo Le Fée": page.locator("#enzo"), "Nordi Mukiele": page.locator("#nordi")}, [],
            )):
                self.assertEqual(
                    parser.read_wina_combination(page, None, ["Enzo Le Fée", "Nordi Mukiele"]), 9.0,
                )

    def test_incorrect_basket_never_returns_the_visible_single_odds(self):
        with parser.wina_page(True) as page:
            page.set_content("""
                <button id="alice" onclick="add()">Alice</button>
                <button id="bob">Bob</button>
                <div data-testid="sticky-wrap">
                  <div id="empty">Ton panier est vide !</div>
                  <div data-testid="basket-item-match" hidden>
                    <div><span>Joueur décisif</span><span> Alice</span></div>
                    <div class="odds-indicator-value"><div>3,20</div></div>
                  </div>
                </div>
                <script>
                  function add() {
                    document.querySelector('#empty').hidden=true;
                    document.querySelector('[data-testid="basket-item-match"]').hidden=false;
                  }
                </script>
            """)
            with patch.object(parser, "extract_wina_players", return_value=(
                {"Alice": page.locator("#alice"), "Bob": page.locator("#bob")}, [],
            )), self.assertRaises(AssertionError):
                parser.read_wina_combination(page, None, ["Alice", "Bob"])

    def test_extracts_real_dom_and_targets_decisive_player_buttons(self):
        teams = []
        for names in (["Alice", "Bob"], ["Dan"]):
            rows = []
            for name in names:
                odds = "".join(
                    f'<button class="odd-button-wrapper" id="{name}-{i}">'
                    f'<span class="odd-button-value">{value}</span></button>'
                    for i, value in enumerate(["1,2", "2,3", "3,4"])
                )
                rows.append(f'<div class="row"><div><span class="label-line">{name}</span></div>{odds}</div>')
            teams.append('<div class="team"><div class="bet-group-template">'
                         + "".join(rows) + '</div></div>')
        with parser.wina_page(True) as page:
            page.set_content("".join(teams))
            buttons, players = parser.extract_wina_players(page.locator("div.bet-group-template"))
            self.assertEqual([p["Nom"] for p in players], ["Alice", "Bob", "Dan"])
            self.assertEqual([p["Cote"] for p in players], [3.4, 3.4, 3.4])
            self.assertEqual([p["home"] for p in players], [1, 1, 0])
            for name, button in buttons.items():
                self.assertEqual(button.get_attribute("id"), f"{name}-2")


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
            "Match", "Equipe", "Taille Combi", "Joueurs", "Cote PMU", "Cote Wina", "Cote Betclic",
            "Ratio (PMU/Wina)", "Ratio (PMU/Betclic)",
        ])
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
                        patch.object(parser, "get_match_lineups", return_value="lineups") as get_lineups, \
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
