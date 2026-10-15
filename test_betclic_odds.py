import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

import betclic_odds as betclic
import master_odds_parser as parser


def players():
    return [
        {"home": 1, "Nom": "Mikael Ishak", "Cote": 2.3},
        {"home": 1, "Nom": "Luis Palma", "Cote": 2.65},
        {"home": 0, "Nom": "Patrik Schick", "Cote": 1.55},
        {"home": 0, "Nom": "Victor Boniface", "Cote": 1.62},
        {"home": 0, "Nom": "Moussa Diaby", "Cote": 1.85},
    ]


def pmu_frame():
    records = [
        {"home": p["home"], "Joueurs": p["Nom"].replace("Patrik", "Patrick"),
         "Taille Combi": 1, "Cote PMU": 3.0, "Equipe": "Home" if p["home"] else "Away"}
        for p in players()
    ]
    records.append({"home": 0, "Joueurs": "Patrick Schick - Victor Boniface",
                    "Taille Combi": 2, "Cote PMU": 5.0, "Equipe": "Away"})
    frame = pd.DataFrame(records)
    frame["Match"] = "Lech - Bayer"
    return frame


class MatchingTests(unittest.TestCase):
    def test_marseille_name_variants_keep_pmu_keys_for_singles_and_duos(self):
        variants = [
            ("Conrad Egan-Riley", "CJ Egan-Riley"),
            ("Faris Pemi Moumbagna", "Faris Moumbagna"),
            ("Marcus Holmgren Pedersen", "Marcus Pedersen"),
            ("Emmanouil Saliakas", "Manolis Saliakas"),
        ]
        records = [{"home": 1, "Nom": name, "Cote": 3.0} for name, _ in variants]
        frame = pd.DataFrame([
            {"home": 1, "Taille Combi": 1, "Joueurs": name} for _, name in variants
        ])
        result = betclic.build_results(records, [
            {"names": ["C. Egan-Riley", "F. Moumbagna"], "Cote": 6.0},
        ], frame)
        self.assertEqual(set(result.loc[result["Taille Combi"] == 1, "Joueurs"]),
                         {"CJ Egan-Riley", "Faris Moumbagna", "Manolis Saliakas", "Marcus Pedersen"})
        self.assertEqual(result.loc[result["Taille Combi"] == 2, "Joueurs"].tolist(),
                         ["CJ Egan-Riley - Faris Moumbagna"])

    def test_shared_matching_preserves_team_scope_and_rejects_ambiguity(self):
        records = [{"home": 1, "Nom": "Conrad Egan-Riley", "Cote": 3.0}]
        frame = pd.DataFrame([{"home": 0, "Taille Combi": 1, "Joueurs": "CJ Egan-Riley"}])
        self.assertEqual(betclic.match_players(records, frame), {})
        frame.loc[0, "home"] = 1
        records.append({"home": 1, "Nom": "Conrad Egan Riley", "Cote": 3.5})
        with self.assertLogs(level="WARNING"):
            self.assertEqual(betclic.match_players(records, frame), {})

    def test_initials_and_extended_initials(self):
        records = players()
        for label, expected in (("P. Schick", 2), ("P.Schick", 2), ("V. Boniface", 3),
                                ("Mo. Diaby", 4), ("Mikael Ishak", 0)):
            with self.subTest(label=label):
                self.assertEqual(betclic.resolve_abbreviation(label, records), expected)

    def test_ambiguous_or_wrong_initial_never_guesses(self):
        records = players() + [{"home": 1, "Nom": "Maxime Diaby", "Cote": 3.0}]
        for label in ("M. Diaby", "X. Schick", "Unknown"):
            with self.subTest(label=label), self.assertLogs(level="WARNING"):
                self.assertIsNone(betclic.resolve_abbreviation(label, records))
        self.assertEqual(betclic.resolve_abbreviation("Mo. Diaby", records), 4)

    def test_abbreviated_surname_and_first_name_produce_canonical_duo(self):
        records = [
            {"home": 1, "Nom": "Marius Mouandilmadji", "Cote": 3.0},
            {"home": 0, "Nom": "Amine Gouiri", "Cote": 2.0},
        ]
        frame = pd.DataFrame([
            {"home": p["home"], "Taille Combi": 1, "Joueurs": p["Nom"]} for p in records
        ])
        result = betclic.build_results(
            records, [{"names": ["Marius M.", "A. Gouiri"], "Cote": 6.0}], frame,
        )
        self.assertEqual(result.loc[result["Taille Combi"] == 2, "Joueurs"].tolist(),
                         ["Amine Gouiri - Marius Mouandilmadji"])

    def test_ambiguous_surname_logs_player_candidates_and_full_duo(self):
        records = [
            {"home": 1, "Nom": "Marius Mouandilmadji", "Cote": 3.0},
            {"home": 0, "Nom": "Marius Martin", "Cote": 4.0},
        ]
        with self.assertLogs(level="WARNING") as logs:
            self.assertIsNone(betclic.resolve_abbreviation(
                "Marius M.", records, "Marius M. & A. Gouiri",
            ))
        text = logs.output[0]
        for expected in ('Joueur ambigu', 'BETCLIC="Marius M."',
                         'duo="Marius M. & A. Gouiri"', "Marius Mouandilmadji", "Marius Martin"):
            self.assertIn(expected, text)
        with self.assertLogs(level="WARNING") as logs:
            self.assertIsNone(betclic.resolve_abbreviation("Unknown", records, "Unknown & A. Gouiri"))
        self.assertIn("Joueur non reconnu", logs.output[0])
        self.assertIn('duo="Unknown & A. Gouiri"', logs.output[0])

    def test_matching_is_team_scoped_one_to_one_and_accepts_spelling_variation(self):
        frame = pmu_frame()
        frame.loc[frame["Joueurs"] == "Luis Palma", "home"] = 0
        mapping = betclic.match_players(players(), frame)
        self.assertNotIn(1, mapping)
        self.assertEqual(mapping[2], "Patrick Schick")
        duplicate = players() + [dict(players()[2])]
        with self.assertLogs(level="WARNING"):
            mapping = betclic.match_players(duplicate, pmu_frame())
        self.assertNotIn("Patrick Schick", mapping.values())

    def test_same_names_have_same_matching_decision_for_both_bookmakers(self):
        frame = pd.DataFrame([
            {"home": 1, "Taille Combi": 1, "Joueurs": "CJ Egan-Riley", "Cote PMU": 8.0},
        ])
        records = [{"home": 1, "Nom": "Conrad Egan-Riley", "Cote": 7.0}]
        for threshold, expected in ((80, True), (84, False), (100, False)):
            with self.subTest(threshold=threshold):
                wina, _, _ = parser.match_wina_players(frame, records, threshold)
                bc = betclic.match_players(records, frame, threshold)
                self.assertEqual(bool(wina), expected)
                self.assertEqual(bool(bc), expected)

    def test_shared_silva_fragment_is_rejected_by_both_bookmakers(self):
        frame = pd.DataFrame([
            {"home": 1, "Taille Combi": 1,
             "Joueurs": "Francisco Leonel Lima Silva Machado", "Cote PMU": 3.0},
        ])
        records = [{"home": 1, "Nom": "Clayton Silva", "Cote": 2.0}]
        self.assertEqual(parser.match_wina_players(frame, records, 80), ({}, {}, []))
        self.assertEqual(betclic.match_players(records, frame), {})

    def test_unmatched_substitute_cannot_become_a_retained_starter(self):
        records = players() + [{"home": 0, "Nom": "Maxime Diaby", "Cote": 3.0}]
        frame = pmu_frame()
        # Moussa remains the best identity for Mo. Diaby even when PMU excludes him.
        frame = frame[frame["Joueurs"] != "Moussa Diaby"]
        frame.loc[len(frame)] = {"home": 0, "Joueurs": "Maxime Diaby",
                                "Taille Combi": 1, "Cote PMU": 3, "Equipe": "Away", "Match": "Lech - Bayer"}
        results = betclic.build_results(
            records, [{"names": ["Mo. Diaby", "P. Schick"], "Cote": 3.5}], frame,
        )
        self.assertTrue(results[results["Taille Combi"] == 2].empty)

    def test_results_use_pmu_keys_and_deduplicate_reverse_pairs(self):
        duos = [
            {"names": ["P. Schick", "V. Boniface"], "Cote": 2.5},
            {"names": ["V. Boniface", "P. Schick"], "Cote": 2.5},
        ]
        results = betclic.build_results(players(), duos, pmu_frame())
        self.assertEqual(len(results), 6)
        duo = results[results["Taille Combi"] == 2].iloc[0]
        self.assertEqual(duo["Joueurs"], "Patrick Schick - Victor Boniface")
        self.assertEqual(duo["Cote Betclic"], 2.5)
        self.assertFalse(results.duplicated(["Taille Combi", "Joueurs"]).any())

    def test_conflicting_duos_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError, "contradictoires"):
            betclic.build_results(players(), [
                {"names": ["P. Schick", "V. Boniface"], "Cote": 2.5},
                {"names": ["V. Boniface", "P. Schick"], "Cote": 3.5},
            ], pmu_frame())

    def test_empty_results_have_expected_schema(self):
        results = betclic.build_results([], [], pmu_frame())
        self.assertTrue(results.empty)
        self.assertEqual(results.columns.tolist(), betclic.RESULT_COLUMNS)

    def test_unavailable_single_odds_do_not_remove_duo_identity(self):
        records = players()
        records[2]["Cote"] = None
        result = betclic.build_results(records, [
            {"names": ["P. Schick", "V. Boniface"], "Cote": 2.5},
        ], pmu_frame())
        self.assertEqual(len(result[result["Taille Combi"] == 2]), 1)
        self.assertFalse(((result["Joueurs"] == "Patrick Schick") & (result["Taille Combi"] == 1)).any())

    def test_odds_validation(self):
        self.assertEqual(betclic.parse_odds(" 2,50\n", "player"), 2.5)
        for text in ("-", "", "0", "1", "-2", "nan", "inf"):
            with self.subTest(text=text), self.assertLogs(level="WARNING"):
                self.assertIsNone(betclic.parse_odds(text, "player"))


def single_row(name, first):
    return (
        f'<div class="marketBox_lineSelection"><p class="marketBox_label">{name}</p>'
        '<div class="marketBox_list">'
        + "".join(f'<div class="marketBox_item"><span class="btn_label">{value}</span></div>'
                  for value in (first, "9,50", "30"))
        + "</div></div>"
    )


class LocalDomTests(unittest.TestCase):
    def test_delayed_cookie_banner_is_closed_before_dialog(self):
        with parser.wina_page(True) as page:
            page.set_content("""
                <div role="dialog"><button onclick="this.parentElement.remove()">Fermer</button></div>
                <div id="cookie" hidden style="position:fixed;inset:0;z-index:1000;background:white">
                    <button onclick="this.parentElement.remove()">Continuer sans accepter</button>
                </div>
                <script>setTimeout(() => document.getElementById('cookie').hidden = false, 2800)</script>
            """)
            betclic.dismiss_optional_dialogs(page)
            self.assertEqual(page.locator("#cookie").count(), 0)
            self.assertEqual(page.get_by_role("dialog").count(), 0)

    def test_cookie_arriving_during_dialog_click_is_retried(self):
        page = MagicMock()
        close = page.get_by_role.return_value.get_by_role.return_value
        close.click.side_effect = [betclic.PlaywrightTimeoutError("cookie overlay"), None]
        with patch.object(betclic, "dismiss_cookie_banner") as dismiss, \
                patch.object(betclic, "expect"):
            betclic.dismiss_optional_dialogs(page)
        self.assertEqual(dismiss.call_count, 2)
        self.assertEqual(close.click.call_count, 2)

    def test_blocked_visible_cookie_banner_is_not_ignored(self):
        page = MagicMock()
        page.get_by_role.return_value.click.side_effect = betclic.PlaywrightTimeoutError("blocked")
        page.get_by_role.return_value.is_visible.return_value = True
        with self.assertRaises(betclic.PlaywrightTimeoutError):
            betclic.dismiss_cookie_banner(page)

    def test_full_navigation_expansion_and_first_column(self):
        home = "".join(single_row(name, "2,30") for name in ("Mikael Ishak", "Luis Palma", "Yannick Agnero"))
        away = "".join(single_row(name, "1,55") for name in ("Patrik Schick", "Victor Boniface", "Moussa Diaby"))
        html = f"""
            <div id="main">
                <div class="marketBox">
                    <h2>{betclic.SINGLES_MARKET}<span> Informations</span></h2>
                    <div class="marketBox_body">{home}</div>
                    <div><button class="is-seeMore" onclick="expandSingles(this, 'Home Extra')"></button></div>
                    <div class="marketBox_body">{away}</div>
                    <div><button class="is-seeMore" onclick="expandSingles(this, 'Away Extra')"></button></div>
                </div>
            </div>
            <button onclick="document.getElementById('main').remove();
                             document.getElementById('tabs').hidden=false">Joueurs</button>
            <div id="tabs" hidden>
                <div role="button" onclick="document.getElementById('duos').hidden=false">Duos &amp; Trios</div>
                <h2>Duos &amp; Trios</h2>
            </div>
            <div id="duos" hidden><div class="marketBox">
                <h2>{betclic.DUOS_MARKET}</h2>
                <div class="marketBox_body is-2col">
                    <div class="marketBox_lineSelection">
                        <p class="marketBox_label">V. Boniface &amp; P. Schick</p><span class="btn_label">2,50</span>
                    </div>
                    <div class="marketBox_lineSelection">
                        <p class="marketBox_label">Mo. Diaby &amp; P. Schick</p><span class="btn_label">2,88</span>
                    </div>
                    <div class="marketBox_lineSelection"></div>
                </div>
                <div><button class="is-seeMore" onclick="expandDuos(this)">Plus</button></div>
            </div></div>
            <script>
                function expandSingles(button, name) {{
                    setTimeout(() => {{
                        const row = document.createElement('div');
                        row.className = 'marketBox_lineSelection';
                        row.innerHTML = '<p class="marketBox_label">' + name
                            + '</p><div class="marketBox_item"><span class="btn_label">4,00</span></div>';
                        button.parentElement.previousElementSibling.appendChild(row);
                        button.classList.add('is-expanded');
                    }}, 100);
                }}
                function expandDuos(button) {{
                    setTimeout(() => {{
                        const row = document.createElement('div');
                        row.className = 'marketBox_lineSelection';
                        row.innerHTML = '<p class="marketBox_label">Mo. Diaby &amp; V. Boniface</p>'
                            + '<span class="btn_label">3,02</span>';
                        button.parentElement.previousElementSibling.appendChild(row);
                        button.classList.add('is-expanded');
                    }}, 100);
                }}
            </script>
        """
        with parser.wina_page(True) as page, patch.object(betclic, "dismiss_optional_dialogs"):
            page.set_content(html)
            records, duos = betclic.extract_markets(page)
        self.assertEqual(len(records), 8)
        self.assertEqual([p["home"] for p in records], [1] * 4 + [0] * 4)
        self.assertEqual(records[0]["Cote"], 2.3)
        self.assertEqual(records[4]["Cote"], 1.55)
        self.assertEqual(duos, [
            {"names": ["V. Boniface", "P. Schick"], "Cote": 2.5},
            {"names": ["Mo. Diaby", "P. Schick"], "Cote": 2.88},
            {"names": ["Mo. Diaby", "V. Boniface"], "Cote": 3.02},
        ])

    def test_expanded_list_is_never_collapsed(self):
        with parser.wina_page(True) as page:
            page.set_content('<div class="row">one</div><button class="is-expanded" onclick="throw Error()">More</button>')
            betclic.expand_selections(page.locator("button"), page.locator(".row"), "test")

    def test_expansion_failure_is_not_silently_accepted(self):
        button, rows = MagicMock(), MagicMock()
        button.get_attribute.return_value = "is-seeMore"
        rows.count.return_value = 3
        button.click.side_effect = betclic.PlaywrightTimeoutError("not clickable")
        with self.assertLogs(level="WARNING"), self.assertRaisesRegex(RuntimeError, "non ouverte"):
            betclic.expand_selections(button, rows, "test")
        self.assertEqual(button.click.call_count, 2)

    def test_expanded_icon_without_added_players_is_a_failure(self):
        button, rows = MagicMock(), MagicMock()
        button.get_attribute.side_effect = ["is-seeMore", "is-seeMore", "is-seeMore is-expanded"]
        rows.count.return_value = 3
        with patch.object(betclic, "expect") as expect, self.assertLogs(level="WARNING"):
            expect.return_value.not_to_have_count.side_effect = AssertionError("no new rows")
            with self.assertRaisesRegex(RuntimeError, "non ouverte"):
                betclic.expand_selections(button, rows, "test")


class PipelineTests(unittest.TestCase):
    def test_existing_sheets_gain_betclic_column_without_shifting_old_values(self):
        frame = pmu_frame().drop(columns="home")
        frame["Cote Wina"] = 2.0
        frame["Cote Betclic"] = 2.5
        frame["Ratio (PMU/Wina)"] = frame["Cote PMU"] / 2
        frame["Ratio (PMU/Betclic)"] = frame["Cote PMU"] / 2.5
        with patch.object(parser.gspread, "service_account") as auth, \
                patch.object(parser, "set_with_dataframe") as write:
            worksheet = auth.return_value.open.return_value.worksheet.return_value
            worksheet.id = 42
            worksheet.col_values.return_value = ["Heure", "old"]
            worksheet.row_values.return_value = ["Heure", *[c for c in frame.columns if c != "Cote Betclic"]]
            parser.export_to_google_sheets(frame, "key", "book", "match")
            worksheet.insert_cols.assert_called_once_with(
                [["Cote Betclic"]], col=frame.columns.get_loc("Cote Betclic") + 2,
            )
            self.assertEqual(write.call_args.kwargs["row"], 3)
            formats = auth.return_value.open.return_value.batch_update.call_args.args[0]["requests"]
            percentage_formats = [
                r["repeatCell"] for r in formats
                if "repeatCell" in r and r["repeatCell"]["cell"]["userEnteredFormat"]["numberFormat"]["type"] == "PERCENT"
            ]
            self.assertEqual(len(percentage_formats), 2)

    def test_incompatible_sheet_header_fails_before_appending(self):
        with patch.object(parser.gspread, "service_account") as auth, \
                patch.object(parser, "set_with_dataframe") as write, self.assertLogs(level="ERROR"):
            worksheet = auth.return_value.open.return_value.worksheet.return_value
            worksheet.col_values.return_value = ["Wrong", "old"]
            worksheet.row_values.return_value = ["Wrong", "columns"]
            with self.assertRaisesRegex(ValueError, "incompatibles"):
                parser.export_to_google_sheets(pmu_frame(), "key", "book", "match")
            write.assert_not_called()

    def test_betclic_is_merged_with_ratios_and_only_retained_pmu_rows(self):
        frame = pmu_frame()
        bc = betclic.build_results(players(), [
            {"names": ["P. Schick", "V. Boniface"], "Cote": 2.5},
        ], frame)
        with patch.object(parser, "GSHEET_NAME", "book", create=True), \
                patch.object(parser, "get_pmu_players_dict", return_value=frame), \
                patch.object(parser, "run_master_wina_and_pmu", return_value=(frame, pd.DataFrame())), \
                patch.object(parser, "run_betclic_extraction", return_value=bc) as extract, \
                patch.object(parser, "export_to_google_sheets") as export:
            args = parser.parse_args(["--name-match-score", "87"])
            parser.process_single_match(123, 456, "https://betclic.test", args, "key", 0)
        extract.assert_called_once()
        self.assertEqual(extract.call_args.args[0], "https://betclic.test")
        self.assertEqual(extract.call_args.args[3], 87)
        result = export.call_args.args[0]
        self.assertEqual(len(result), len(frame))
        duo = result[result["Taille Combi"] == 2].iloc[0]
        self.assertEqual(duo["Cote Betclic"], 2.5)
        self.assertEqual(duo["Ratio (PMU/Betclic)"], 2.0)
        self.assertTrue(result["Cote Wina"].isna().all())

    def test_empty_csv_url_skips_betclic_and_retains_pmu_rows(self):
        frame = pmu_frame()
        for url in ("", None, float("nan")):
            with self.subTest(url=url), patch.object(parser, "GSHEET_NAME", "book", create=True), \
                    patch.object(parser, "get_pmu_players_dict", return_value=frame), \
                    patch.object(parser, "run_master_wina_and_pmu", return_value=(frame, pd.DataFrame())), \
                    patch.object(parser, "run_betclic_extraction") as extract, \
                    patch.object(parser, "export_to_google_sheets") as export:
                parser.process_single_match(123, 456, url, parser.parse_args([]), "key", 0)
                extract.assert_not_called()
                result = export.call_args.args[0]
                self.assertEqual(len(result), len(frame))
                self.assertTrue(result["Cote Betclic"].isna().all())
                self.assertTrue(result["Ratio (PMU/Betclic)"].isna().all())

    def test_missing_markets_keep_nan_but_extraction_errors_propagate(self):
        frame = pmu_frame()
        with patch.object(parser, "GSHEET_NAME", "book", create=True), \
                patch.object(parser, "get_pmu_players_dict", return_value=frame), \
                patch.object(parser, "run_master_wina_and_pmu", return_value=(frame, pd.DataFrame())), \
                patch.object(parser, "run_betclic_extraction",
                             return_value=pd.DataFrame(columns=betclic.RESULT_COLUMNS)) as extract, \
                patch.object(parser, "export_to_google_sheets") as export:
            parser.process_single_match(123, 456, "https://betclic.test", parser.parse_args([]), "key", 0)
            self.assertTrue(export.call_args.args[0]["Cote Betclic"].isna().all())
            export.reset_mock()
            extract.side_effect = RuntimeError("could not expand Betclic")
            with self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "expand Betclic"):
                parser.process_single_match(123, 456, "https://betclic.test", parser.parse_args([]), "key", 0)
            export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
