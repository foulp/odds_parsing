from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from name_matching import (
    load_aliases, match_names, name_score, normalize_name, matches_abbreviation,
)


class NameMatchingTests(unittest.TestCase):
    def test_unique_middle_name_omission_works_in_both_directions(self):
        for reference, candidate in (
            ("Marcus Pedersen", "Marcus Holmgren Pedersen"),
            ("Marcus Holmgren Pedersen", "Marcus Pedersen"),
            ("Faris Moumbagna", "Faris Pemi Moumbagna"),
        ):
            with self.subTest(reference=reference), self.assertLogs(level="INFO") as logs:
                self.assertEqual(match_names([reference], [candidate]), {0: (0, 100)})
            self.assertIn("methode=NOMS_INTERMEDIAIRES_OMIS", " ".join(logs.output))

    def test_middle_omission_requires_unique_endpoints_in_complete_lists(self):
        short = "Marcus Pedersen"
        full = "Marcus Holmgren Pedersen"
        other = "Marcus Alexander Pedersen"
        for references, candidates in (
            ([short], [full, other]),
            ([full, other], [short]),
            ([full, short], [full, other]),
        ):
            with self.subTest(references=references), self.assertLogs(level="INFO") as logs:
                match_names(references, candidates)
            self.assertNotIn("methode=NOMS_INTERMEDIAIRES_OMIS", " ".join(logs.output))

    def test_middle_omission_does_not_ignore_conflicting_middle_names(self):
        for reference, candidate in (
            ("Marcus Holmgren Pedersen", "Marcus Alexander Pedersen"),
            ("Marcus Pedersen", "Markus Holmgren Pedersen"),
            ("Marcus Pedersen", "Marcus Holmgren Petersen"),
        ):
            with self.subTest(candidate=candidate), self.assertLogs(level="INFO") as logs:
                match_names([reference], [candidate], min_score=100)
            self.assertNotIn("methode=NOMS_INTERMEDIAIRES_OMIS", " ".join(logs.output))

    def test_unique_single_word_name_is_accepted_in_either_direction(self):
        for reference, candidate in (
            ("Emerson Palmieri", "Emerson"),
            ("Emerson", "Emerson Palmieri"),
        ):
            with self.subTest(reference=reference), self.assertLogs(level="INFO") as logs:
                self.assertEqual(match_names([reference], [candidate]), {0: (0, 100)})
            self.assertIn("methode=NOM_COURT_UNIQUE", " ".join(logs.output))

    def test_short_name_must_be_unique_in_both_complete_lists(self):
        self.assertEqual(match_names(["Emerson Palmieri", "Emerson Royal"], ["Emerson"]), {})
        self.assertEqual(match_names(["Emerson"], ["Emerson Palmieri", "Emerson Royal"]), {})
        matches = match_names(
            ["Emerson Palmieri", "Emerson Royal"], ["Emerson Palmieri", "Emerson"],
        )
        self.assertEqual(matches, {0: (0, 100)})

    def test_short_name_requires_a_whole_word_not_a_fragment(self):
        self.assertEqual(match_names(["Emerson Palmieri"], ["Emer"]), {})
        self.assertEqual(match_names(["Emerson Palmieri"], ["E."]), {})

    def test_short_name_must_be_the_first_word_in_either_direction(self):
        for full_name, short in (
            ("Emerson Palmieri", "Palmieri"),
            ("Francisco Leonel Lima Silva Machado", "Silva"),
        ):
            for reference, candidate in ((full_name, short), (short, full_name)):
                with self.subTest(reference=reference, candidate=candidate):
                    self.assertEqual(match_names([reference], [candidate]), {})

    def test_abbreviations_can_shorten_first_or_last_name(self):
        for label, full_name in (
            ("A. Gouiri", "Amine Gouiri"),
            ("Marius M.", "Marius Mouandilmadji"),
            ("Mo. Diaby", "Moussa Diaby"),
            ("C. Egan-Riley", "Conrad Egan Riley"),
            ("F. Moumbagna", "Faris Pemi Moumbagna"),
            ("J. P. Silva", "Joao Pedro Silva"),
        ):
            with self.subTest(label=label):
                self.assertTrue(matches_abbreviation(label, full_name))
        for label, full_name in (
            ("Marius M.", "Marius Diaby"),
            ("Mario M.", "Marius Mouandilmadji"),
            ("X. Gouiri", "Amine Gouiri"),
            ("A. G.", "Amine Gouiri"),
            ("", ""),
        ):
            with self.subTest(label=label):
                self.assertFalse(matches_abbreviation(label, full_name))

    def test_normalization_handles_case_accents_hyphens_and_whitespace(self):
        self.assertEqual(normalize_name("  CONRÁD  Egan-Riley "), "conrad egan riley")
        self.assertEqual(name_score("Conrad Egan-Riley", "CONRAD Egan Riley"), 100)

    def test_common_score_and_threshold_without_player_aliases(self):
        self.assertEqual(name_score("CJ Egan-Riley", "Conrad Egan Riley"), 80)
        self.assertEqual(match_names(["CJ Egan-Riley"], ["Conrad Egan Riley"]), {0: (0, 80)})
        self.assertEqual(match_names(["CJ Egan-Riley"], ["Conrad Egan Riley"], 84), {})

    def test_exact_match_precedes_fuzzy_and_candidates_are_not_reused(self):
        with self.assertLogs(level="INFO") as logs:
            matches = match_names(["Alicia", "Alice"], ["Alice"], 50)
        self.assertEqual(matches, {1: (0, 100)})
        self.assertIn("tous les candidats sont deja attribues", " ".join(logs.output))
        self.assertNotIn("AMBIGU", " ".join(logs.output))

    def test_assigned_candidate_is_excluded_before_ranking_remaining_names(self):
        matches = match_names(["Alicia Smit", "Alice"], ["Alice", "Alicia Smith"], 80)
        self.assertEqual(matches[1], (0, 100))
        self.assertEqual(matches[0][0], 1)

    def test_logs_identify_bookmakers_and_explain_rejections(self):
        for bookmaker in ("WINAMAX", "BETCLIC"):
            with self.subTest(bookmaker=bookmaker), self.assertLogs(level="INFO") as logs:
                match_names(["CJ Egan-Riley"], ["Conrad Egan Riley"], label=bookmaker)
                match_names(["CJ Egan-Riley"], ["Conrad Egan Riley"], 90, label=bookmaker)
            text = " ".join(logs.output)
            self.assertIn('PMU="CJ Egan-Riley"', text)
            self.assertIn(f'{bookmaker}="Conrad Egan Riley"', text)
            self.assertIn("score inferieur au seuil", text)
            self.assertIn("seuil=90", text)

    def test_ambiguous_candidates_are_not_chosen_arbitrarily(self):
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(match_names(["Alice"], ["Alice", "ALICE"]), {})
        self.assertIn('NOMS="Alice" (score 100), NOMS="ALICE" (score 100)', logs.output[0])
        self.assertIn("ecart=0 < 5", logs.output[0])
        self.assertEqual(match_names([""], ["Alice"]), {})
        self.assertEqual(match_names(["Alice"], []), {})

    def test_shared_surname_does_not_produce_a_false_positive(self):
        left, right = "Francisco Leonel Lima Silva Machado", "Clayton Silva"
        self.assertEqual(name_score(left, right), 42)
        self.assertEqual(match_names([left], [right]), {})

    def test_matching_methods_are_logged_and_abbreviations_precede_fuzzy(self):
        for reference, candidate, method in (
            ("Conrad Egan Riley", "Conrad Egan-Riley", "EXACT"),
            ("A. Gouiri", "Amine Gouiri", "ABREVIATION"),
            ("Amine Gouiri", "A. Gouiri", "ABREVIATION"),
            ("Marius M.", "Marius Mouandilmadji", "ABREVIATION"),
            ("CJ Egan-Riley", "Conrad Egan Riley", "FUZZY_GLOBAL"),
        ):
            with self.subTest(method=method), self.assertLogs(level="INFO") as logs:
                self.assertTrue(match_names([reference], [candidate]))
            self.assertIn(f"methode={method}", " ".join(logs.output))
        with self.assertLogs(level="INFO"):
            matches = match_names(["Amine Gouir", "A. Gouiri"], ["Amine Gouiri"])
        self.assertEqual(matches, {1: (0, 100)})

    def test_abbreviation_ambiguity_is_rejected(self):
        with self.assertLogs(level="WARNING"):
            self.assertEqual(match_names(["A. Gouiri"], ["Amine Gouiri", "Adam Gouiri"]), {})

    def test_birth_date_suffix_is_ignored(self):
        self.assertEqual(normalize_name("Nico González (03/01/2002)"), "nico gonzalez")
        self.assertTrue(matches_abbreviation("C. Eduardo (10/08/2002)", "Carlos Eduardo (10/02/2002)"))
        with self.assertLogs(level="INFO"):
            self.assertEqual(match_names(["Nico Gonzalez"], ["Nico González (03/01/2002)"]),
                             {0: (0, 100)})

    def test_homonyms_without_date_stay_ambiguous(self):
        with self.assertLogs(level="WARNING"):
            self.assertEqual(match_names(
                ["Carlos Eduardo"], ["Carlos Eduardo (10/02/2002)", "Carlos Eduardo (01/01/1999)"],
            ), {})

    def test_alias_file_links_different_first_names(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.csv"
            path.write_text("# PMU;bookmaker\n\nCaumenan N'Guessan;Ange N'Guessan\n",
                            encoding="utf-8")
            aliases = load_aliases(path)
        with self.assertLogs(level="INFO") as logs:
            matches = match_names(["Caumenan N'Guessan", "David Zima"],
                                  ["Ange N'Guessan", "David Zima"], 100, aliases=aliases)
        self.assertEqual(matches, {0: (0, 100), 1: (1, 100)})
        self.assertIn("methode=ALIAS", "\n".join(logs.output))

    def test_without_alias_different_first_names_stay_unmatched(self):
        with self.assertLogs(level="INFO"):
            self.assertEqual(match_names(["Caumenan N'Guessan"], ["Ange N'Guessan"], aliases={}), {})

    def test_missing_or_invalid_alias_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.csv"
            self.assertEqual(load_aliases(path), {})
            path.write_text("Only one name\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ligne 1"):
                load_aliases(path)



if __name__ == "__main__":
    unittest.main()
