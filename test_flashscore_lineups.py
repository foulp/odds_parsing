from datetime import date
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

import flashscore_lineups as flashscore
from master_odds_parser import wina_page


def player(name, slug):
    return {"name": name, "href": f"/joueur/{slug}/id", "goalkeeper": False}


class MatchingTests(unittest.TestCase):
    def test_bookmaker_team_aliases_match_flashscore_names(self):
        matches = [
            {"home": "Manchester City", "away": "PSG", "url": "city-psg"},
            {"home": "AS Rome", "away": "Real Madrid", "url": "roma-real"},
            {"home": "Manchester Utd", "away": "PSG", "url": "united-psg"},
        ]
        for home, away, expected in (
            ("Manchester City", "Paris SG", "city-psg"),
            ("Manchester City", "Paris Saint-Germain", "city-psg"),
            ("Roma", "Real Madrid", "roma-real"),
            ("AS Roma", "Real Madrid", "roma-real"),
        ):
            with self.subTest(home=home, away=away):
                self.assertEqual(flashscore.find_match(matches, home, away)["url"], expected)

    def test_bruges_names_of_every_site_are_one_team(self):
        matches = [{"home": "Club Brugge", "away": "Lens", "url": "brugge-lens"}]
        for home in ("FC Bruges", "Club Bruges", "Club Brugge"):
            with self.subTest(home=home):
                self.assertEqual(flashscore.find_match(matches, home, "Lens")["url"], "brugge-lens")

    def test_aliases_do_not_remove_category_suffixes(self):
        self.assertEqual(flashscore.normalize_team("Paris SG"), "psg")
        self.assertNotEqual(flashscore.normalize_team("Paris SG U19"), "psg")
        self.assertNotEqual(flashscore.normalize_team("Roma F"), "as rome")

    def test_alias_matching_still_rejects_ambiguous_candidates(self):
        matches = [
            {"home": "Manchester City", "away": "PSG", "url": "one"},
            {"home": "Manchester City", "away": "Paris SG", "url": "two"},
        ]
        with self.assertRaisesRegex(ValueError, "ambigu"):
            flashscore.find_match(matches, "Manchester City", "Paris SG")

    def test_abbreviations_accents_aliases_and_slug_names(self):
        cases = [
            (player("Nyoni T.", "nyoni-trey"), "Treymaurice Nyoni"),
            (player("Tsimikas K.", "tsimikas-konstantinos"), "Kostas Tsimikas"),
            (player("Molgaard Jorgensen K. P.", "jorgensen-kasper"), "Kasper Jørgensen"),
            (player("Tornich J.", "alemao"), "Joao Victor Tornich"),
            (player("Mbappé K.", "mbappe-kylian"), "Kylian Mbappé"),
        ]
        for announced, pmu in cases:
            with self.subTest(pmu=pmu):
                self.assertGreaterEqual(flashscore.player_score(announced, pmu), 85)

    def test_team_scoping_and_missing_market_players(self):
        frame = pd.DataFrame([
            {"home": 1, "Joueurs": "Alice Smith"},
            {"home": 1, "Joueurs": "Home Substitute"},
            {"home": 0, "Joueurs": "Bob Smith"},
            {"home": 0, "Joueurs": "Away Substitute"},
        ], index=["a", "b", "c", "d"])
        lineups = {
            1: [player("Smith A.", "smith-alice"), player("Missing X.", "missing-xavier")],
            0: [player("Smith B.", "smith-bob")],
        }
        with self.assertLogs(level="WARNING"):
            result = flashscore.filter_players(frame, lineups)
        self.assertEqual(result["Joueurs"].tolist(), ["Alice Smith", "Bob Smith"])

    def test_ambiguous_name_is_excluded_and_logs_all_candidates(self):
        frame = pd.DataFrame([
            {"home": 1, "Joueurs": "John Smith"},
            {"home": 1, "Joueurs": "James Smith"},
            {"home": 1, "Joueurs": "Alice Brown"},
            {"home": 0, "Joueurs": "Bob Jones"},
        ])
        with self.assertLogs(level="WARNING") as logs:
            result = flashscore.filter_players(frame, {
                1: [player("Smith J.", "smith"), player("Brown A.", "brown-alice")],
                0: [player("Jones B.", "jones-bob")],
            })
        self.assertEqual(result["Joueurs"].tolist(), ["Alice Brown", "Bob Jones"])
        for name in ("Smith J.", "John Smith", "James Smith", "100"):
            self.assertIn(name, " ".join(logs.output))

    def test_duplicate_targets_exclude_all_claimants_independent_of_order(self):
        frame = pd.DataFrame([
            {"home": 1, "Joueurs": "John Smith"},
            {"home": 1, "Joueurs": "Alice Brown"},
            {"home": 0, "Joueurs": "Bob Jones"},
        ])
        claimants = [player("Smith J.", "smith-john"), player("Smith John", "smith-john")]
        for order in (claimants, list(reversed(claimants))):
            with self.assertLogs(level="WARNING") as logs:
                result = flashscore.filter_players(frame, {
                    1: order + [player("Brown A.", "brown-alice")],
                    0: [player("Jones B.", "jones-bob")],
                })
            self.assertEqual(result["Joueurs"].tolist(), ["Alice Brown", "Bob Jones"])
            for name in ("Smith J.", "Smith John", "John Smith"):
                self.assertIn(name, " ".join(logs.output))

    def test_coventry_ambiguity_keeps_only_reliable_matches(self):
        names = ["Bobby Thomas", "Brandon Thomas-Asante", "Milan Van Ewijk", "Matt Grimes",
                 "Jack Rudoni", "Ellis Simms", "Tatsuhiro Sakamoto", "Liam Kitching",
                 "Jay Dasilva", "Victor Torp"]
        frame = pd.DataFrame(
            [{"home": 0, "Joueurs": name, "Equipe": "Coventry"} for name in names]
            + [{"home": 1, "Joueurs": "Alice Brown", "Equipe": "Tottenham"}],
        )
        lineups = {
            0: [player("Thomas B.", "thomas-bobby"),
                player("Thomas-Asante B.", "thomas-asante-brandon")]
                + [player(name, name.replace(" ", "-")) for name in names[2:]],
            1: [player("Brown A.", "brown-alice")],
        }
        with self.assertLogs(level="INFO") as logs:
            result = flashscore.filter_players(frame, lineups)
        self.assertEqual(result.loc[result["home"] == 0, "Joueurs"].tolist(), names[1:])
        self.assertIn("[Coventry] 9/10", " ".join(logs.output))
        ambiguous_frame = pd.concat([
            frame, pd.DataFrame([{"home": 0, "Joueurs": "Ben Thomas-Asante", "Equipe": "Coventry"}]),
        ], ignore_index=True)
        with self.assertLogs(level="INFO") as logs:
            result = flashscore.filter_players(ambiguous_frame, lineups)
        self.assertEqual(result.loc[result["home"] == 0, "Joueurs"].tolist(), names[2:])
        self.assertIn("[Coventry] 8/10", " ".join(logs.output))

    def test_no_recognized_starter_fails(self):
        frame = pd.DataFrame([{"home": 1, "Joueurs": "Substitute"}])
        with self.assertLogs(level="WARNING"), self.assertRaisesRegex(ValueError, "Aucun titulaire"):
            flashscore.filter_players(frame, {1: [player("Smith J.", "smith-john")], 0: []})

    def test_find_match_respects_home_and_away(self):
        matches = [
            {"home": "LASK", "away": "Liverpool", "url": "correct"},
            {"home": "Liverpool", "away": "LASK", "url": "reversed"},
        ]
        self.assertEqual(flashscore.find_match(matches, "LASK", "Liverpool")["url"], "correct")
        with self.assertRaisesRegex(ValueError, "introuvable"):
            flashscore.find_match(matches, "Paris Saint Germain", "Marseille")
        with self.assertRaisesRegex(ValueError, "ambigu"):
            flashscore.find_match([matches[0], matches[0]], "LASK", "Liverpool")

    def test_dates_and_composition_url(self):
        today = date(2026, 10, 14)
        for label in ("18:45", "21:00", "14.10. 18:45"):
            self.assertTrue(flashscore.is_today(label, today))
        for label in ("20.10. 18:45", "", "13.10. 21:00"):
            self.assertFalse(flashscore.is_today(label, today))
        self.assertEqual(
            flashscore.composition_url("https://www.flashscore.fr/match/football/a/b/?mid=abc"),
            "https://www.flashscore.fr/match/football/a/b/resume/compositions/?mid=abc",
        )


def group_html(heading, prefix="starter", count=11, goalkeeper=True):
    columns = []
    for side in ("left", "right"):
        rows = []
        for i in range(count):
            role = '<span title="Gardien">(G)</span>' if i == 0 and goalkeeper else ""
            rows.append(
                f'<div data-testid="wcl-lineupsParticipantGeneral-{side}">'
                f'<a href="/joueur/{prefix}-{side}-{i}/id{i}">'
                f'<span>{prefix} {side} {i}</span>{role}</a></div>'
            )
        columns.append("<div>" + "".join(rows) + "</div>")
    return ('<div data-testid="wcl-lineupsGroup"><h2>' + heading + "</h2>"
            + "".join(columns) + "</div>")


class LocalDomTests(unittest.TestCase):
    def test_calendar_counts_identify_each_competition(self):
        urls = [
            ("LDC", flashscore.COMPETITION_URL, "15.10. 21:00", 1),
            ("L1", "https://www.flashscore.fr/football/france/ligue-1", "16.10. 21:00", 0),
            ("PL", "https://www.flashscore.fr/football/angleterre/premier-league", "16.10. 21:00", 0),
        ]
        with wina_page(True) as page:
            for label, url, kickoff, count in urls:
                html = (
                    '<div class="event__match"><span class="event__time">' + kickoff + '</span>'
                    '<span class="event__homeParticipant">Home</span>'
                    '<span class="event__awayParticipant">Away</span>'
                    '<a href="/match/fixture">Match</a></div>'
                )
                page.route("https://www.flashscore.fr/**", lambda route: route.fulfill(
                    content_type="text/html", body=html,
                ))
                with self.assertLogs(level="INFO") as logs:
                    result = flashscore.discover_matches(
                        page, date(2026, 10, 15), url, allow_empty=True,
                    )
                self.assertEqual(len(result), count)
                self.assertIn(f"[FLASHSCORE][{label}] {count} matchs", " ".join(logs.output))
                page.unroute("https://www.flashscore.fr/**")

    def test_empty_competition_calendar_is_normal_but_not_a_blank_page(self):
        with wina_page(True) as page:
            page.route("https://www.flashscore.fr/**", lambda route: route.fulfill(
                content_type="text/html", body='<div class="noMatchFound">Aucun match</div>',
            ))
            self.assertEqual(flashscore.discover_matches(page, allow_empty=True), [])

    def test_probable_and_announced_lineups_exclude_keepers_and_substitutes(self):
        with wina_page(True) as page:
            for heading in ("Compositions probables", "Compositions de départ"):
                with self.subTest(heading=heading):
                    html = group_html(heading) + group_html("Remplaçants", "substitute")
                    page.route("https://fixture.test/**", lambda route: route.fulfill(
                        content_type="text/html; charset=utf-8", body=html,
                    ))
                    lineups = flashscore.read_lineups(page, "https://fixture.test/match/a?mid=1",
                                                      allow_probable=True)
                    self.assertEqual(lineups.kind, "probables" if "probables" in heading
                                     else "officielles")
                    self.assertEqual(set(lineups), {1, 0})
                    for home, side in ((1, "left"), (0, "right")):
                        self.assertEqual(len(lineups[home]), 10)
                        self.assertEqual(lineups[home][0]["name"], f"starter {side} 1")
                        self.assertTrue(all("substitute" not in p["name"] for p in lineups[home]))
                    page.unroute("https://fixture.test/**")

    def test_unmarked_keeper_is_not_silently_discarded(self):
        with wina_page(True) as page:
            page.route("https://fixture.test/**", lambda route: route.fulfill(
                content_type="text/html",
                body=group_html("Compositions probables", goalkeeper=False),
            ))
            with self.assertRaisesRegex(ValueError, "Gardien"):
                flashscore.read_lineups(page, "https://fixture.test/match/a", allow_probable=True)

    def test_probable_lineups_do_not_filter_by_default(self):
        with wina_page(True) as page:
            page.route("https://fixture.test/**", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8", body=group_html("Compositions probables"),
            ))
            self.assertIsNone(flashscore.read_lineups(page, "https://fixture.test/match/a"))

    def test_official_lineup_is_preferred_even_when_probable_is_allowed(self):
        with wina_page(True) as page:
            page.route("https://fixture.test/**", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8",
                body=group_html("Compositions probables", "probable")
                + group_html("Compositions de départ", "official"),
            ))
            result = flashscore.read_lineups(page, "https://fixture.test/match/a", allow_probable=True)
            self.assertTrue(result[1][0]["name"].startswith("official"))
            self.assertEqual(result.kind, "officielles")

    def test_missing_groups_only_count_as_absent_on_loaded_match_without_compo_tab(self):
        page = MagicMock()
        page.locator.return_value.first.wait_for.side_effect = flashscore.PlaywrightTimeoutError("absent")
        page.locator.return_value.count.return_value = 0
        self.assertIsNone(flashscore.read_lineups(page, "https://fixture.test/match/a"))
        page.locator.return_value.count.return_value = 1
        with self.assertRaises(flashscore.PlaywrightTimeoutError):
            flashscore.read_lineups(page, "https://fixture.test/match/a")

    def test_discovery_excludes_other_dates_and_preserves_sides(self):
        html = "".join(
            f'<div class="event__match"><span class="event__stageTime">{time}</span>'
            f'<div class="event__homeParticipant">{home}</div>'
            f'<div class="event__awayParticipant">{away}</div>'
            '<a href="https://www.flashscore.fr/match/football/a/b/?mid=1"></a></div>'
            for time, home, away in (("18:45", "LASK", "Liverpool"),
                                     ("20.10. 18:45", "Future Home", "Future Away"))
        )
        with wina_page(True) as page:
            page.route("https://www.flashscore.fr/**", lambda route: route.fulfill(
                content_type="text/html", body=html,
            ))
            matches = flashscore.discover_matches(page, date(2026, 10, 14))
        self.assertEqual(len(matches), 1)
        self.assertEqual((matches[0]["home"], matches[0]["away"]), ("LASK", "Liverpool"))

    def test_incomplete_lineup_does_not_fall_back(self):
        page = MagicMock()
        with patch.object(flashscore, "expect") as expect:
            expect.return_value.to_have_count.side_effect = AssertionError("Only 8 players")
            with self.assertRaisesRegex(AssertionError, "Only 8"):
                flashscore.read_lineups(page, "https://fixture.test/match/a")


if __name__ == "__main__":
    unittest.main()
