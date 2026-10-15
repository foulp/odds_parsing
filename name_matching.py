"""Matching commun : exact, abreviation/nom court unique, puis fuzzy global."""

import logging
import re

from thefuzz import fuzz
from unidecode import unidecode


DEFAULT_MATCH_SCORE = 80


def normalize_name(value):
    return " ".join(re.findall(r"[a-z0-9]+", unidecode(str(value)).lower()))


def name_score(left, right):
    return fuzz.token_sort_ratio(normalize_name(left), normalize_name(right))


def matches_abbreviation(label, full_name):
    """Verifie les mots complets et les prefixes marques d'un point, sans fuzzy.

    Les abreviations peuvent porter sur le prenom ou le nom. Les prenoms
    intermediaires peuvent etre omis avant un nom de famille ecrit en entier.
    """
    if normalize_name(label) == normalize_name(full_name):
        return bool(normalize_name(label))
    parts = re.findall(r"[^\W\d_]+\.?", label, re.UNICODE)
    tokens = normalize_name(full_name).split()
    if not parts or not any(not part.endswith(".") for part in parts):
        return False
    if not any(part.endswith(".") for part in parts):
        return False
    if len(parts) == len(tokens):
        return all(
            token.startswith(normalize_name(part)) if part.endswith(".")
            else token == normalize_name(part)
            for part, token in zip(parts, tokens)
        )
    prefixes = []
    while parts and parts[0].endswith("."):
        prefixes.append(normalize_name(parts.pop(0)))
    if not prefixes or not parts or any(part.endswith(".") for part in parts):
        return False
    surname = normalize_name(" ".join(parts)).split()
    return (
        len(tokens) >= len(prefixes) + len(surname)
        and tokens[-len(surname):] == surname
        and all(token.startswith(prefix) for prefix, token in zip(prefixes, tokens))
    )


def match_names(reference_names, candidate_names, min_score=DEFAULT_MATCH_SCORE, label="NOMS"):
    """Retourne index reference -> (index candidat, score), sans reutilisation.

    Les noms exacts puis les abreviations passent avant le fuzzy global.
    Un nom d'un seul mot doit etre le premier mot du nom complet et identifier
    un mot entier unique dans les deux listes, avant toute exclusion de candidats.
    Un nom de deux mots peut omettre des mots intermediaires si ses premier et
    dernier mots sont identiques et cette paire est unique dans les deux listes.
    Les candidats attribues sont exclus.
    Un ecart inferieur a cinq points entre les deux meilleurs candidats restants
    est refuse, meme au-dessus du seuil. Les references sont les noms PMU.
    """
    references = [normalize_name(name) for name in reference_names]
    candidates = [normalize_name(name) for name in candidate_names]

    def unique_short_name(left, right):
        left_tokens, right_tokens = left.split(), right.split()
        if len(left_tokens) == 1 and len(right_tokens) > 1:
            short = left
        elif len(right_tokens) == 1 and len(left_tokens) > 1:
            short = right
        else:
            return False
        return (
            len(short) > 1
            and short == left_tokens[0] and short == right_tokens[0]
            and sum(short in name.split() for name in references) == 1
            and sum(short in name.split() for name in candidates) == 1
        )

    def unique_middle_omission(left, right):
        left_tokens, right_tokens = left.split(), right.split()
        if not ((len(left_tokens) == 2 and len(right_tokens) > 2)
                or (len(right_tokens) == 2 and len(left_tokens) > 2)):
            return False
        endpoints = (left_tokens[0], left_tokens[-1])
        if (endpoints != (right_tokens[0], right_tokens[-1])
                or any(len(word) < 2 for word in endpoints)):
            return False

        def count_names(names):
            return sum(
                len(tokens) >= 2 and (tokens[0], tokens[-1]) == endpoints
                for tokens in (name.split() for name in names)
            )

        return count_names(references) == 1 and count_names(candidates) == 1

    def comparison(i, j):
        if references[i] and references[i] == candidates[j]:
            return 0, 100, "EXACT"
        if matches_abbreviation(reference_names[i], candidate_names[j]) or matches_abbreviation(
            candidate_names[j], reference_names[i],
        ):
            return 1, 100, "ABREVIATION"
        if unique_short_name(references[i], candidates[j]):
            return 1, 100, "NOM_COURT_UNIQUE"
        if unique_middle_omission(references[i], candidates[j]):
            return 1, 100, "NOMS_INTERMEDIAIRES_OMIS"
        return 2, name_score(references[i], candidates[j]), "FUZZY_GLOBAL"

    comparisons = {
        (i, j): comparison(i, j)
        for i in range(len(references)) for j in range(len(candidates))
    }
    order = sorted(
        range(len(references)),
        key=lambda i: min((comparisons[i, j][0] for j in range(len(candidates))), default=2),
    )
    used = set()
    matches = {}
    for i in order:
        ranked = sorted(
            ((comparisons[i, j][1], j)
             for j, name in enumerate(candidates) if j not in used),
            key=lambda item: (comparisons[i, item[1]][0], -item[0]),
        ) if references[i] else []
        if ranked:
            best_level = comparisons[i, ranked[0][1]][0]
            ranked = [item for item in ranked if comparisons[i, item[1]][0] == best_level]
        if not ranked or ranked[0][0] < min_score:
            closest = (
                f'{label}="{candidate_names[ranked[0][1]]}" (score {ranked[0][0]})'
                if ranked else "aucun candidat disponible"
            )
            reason = (
                "nom PMU vide" if not references[i]
                else "tous les candidats sont deja attribues" if candidates and not ranked
                else "aucun candidat fourni" if not candidates
                else "score inferieur au seuil"
            )
            method = comparisons[i, ranked[0][1]][2] if ranked else "AUCUNE"
            logging.info('[%s] NON RECONNU : PMU="%s" ; %s ; methode=%s ; seuil=%s ; meilleur candidat : %s',
                         label, reference_names[i], reason, method, min_score, closest)
            continue
        score, j = ranked[0]
        if len(ranked) > 1 and score - ranked[1][0] < 5:
            second_score, second = ranked[1]
            logging.warning(
                '[%s] AMBIGU : PMU="%s" ; %s="%s" (score %s), %s="%s" (score %s) ; '
                'ecart=%s < 5 ; seuil=%s ; methode=%s ; correspondance refusee',
                label, reference_names[i], label, candidate_names[j], score,
                label, candidate_names[second], second_score, score - second_score, min_score,
                comparisons[i, j][2],
            )
            continue
        used.add(j)
        matches[i] = (j, score)
        logging.info('[%s] MATCH OK : PMU="%s" <-> %s="%s" ; methode=%s ; score=%s ; seuil=%s',
                     label, reference_names[i], label, candidate_names[j],
                     comparisons[i, j][2], score, min_score)
    return matches
