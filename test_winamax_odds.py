import json
import math
import unittest
from unittest.mock import MagicMock

from playwright.sync_api import Error as PlaywrightError, sync_playwright

import winamax_odds as wina


URL = "https://www.winamax.fr/paris-sportifs/match/7"
SOCKET = "wss://sports.test/socket.io/?EIO=3&transport=polling&sid=abc"
MATCH_PAYLOAD = {
    "matches": {"7": {"matchId": 7, "status": "PREMATCH"}},
    "bets": {
        "70": {"betId": 70, "matchId": 7, "betTitle": "Joueur décisif",
               "available": True, "outcomes": [1, 2, 3, 4]},
        "71": {"betId": 71, "matchId": 7, "betTitle": "Joueur décisif 2 fois ou plus",
               "available": True, "outcomes": [9]},
    },
    "outcomes": {
        "1": {"label": "Harry Kane", "team": 1, "available": True},
        "2": {"label": " Luis  Diaz ", "team": 1, "available": True},
        "3": {"label": "Kai Havertz", "team": 2, "available": False},
        "4": {"label": "Bukayo Saka", "team": 2, "available": True},
        "9": {"label": "Harry Kane", "team": 1, "available": True},
    },
    "odds": {"1": 1.68, "2": 1.94, "3": 2.1, "4": 1.0, "9": 5.0},
}

# Faux serveur socket.io (EIO=3) : handshake, route du match, GetOdds.
FAKE_SOCKET = """payload => {
    window.__sent = [];
    window.__urls = [];
    window.__payload = payload;
    window.WebSocket = class {
        constructor(url) {
            this.readyState = 0;
            window.__urls.push(url);
            window.__socket = this;
            setTimeout(() => {
                this.readyState = 1;
                this.emit('0{"sid":"x","pingInterval":25000}');
                this.emit('40');
            }, 5);
        }
        emit(text) { if (this.readyState === 1) this.onmessage({data: text}); }
        push(message) { setTimeout(() => this.emit('42' + JSON.stringify(['m', message])), 5); }
        send(text) {
            window.__sent.push(text);
            if (!text.startsWith('42')) return;
            const message = JSON.parse(text.slice(2))[1];
            if (message.route) this.push(window.__payload);
            if (message.myMatch) {
                const ids = message.myMatch.params.outcomes.map(o => o.outcomeId);
                this.push({myMatchResults: {id: message.myMatch.id, result: {
                    mymatchOdds: 1 + ids.length,
                    subOutcomesOdds: Object.fromEntries(ids.map(id => [id, 1.5])),
                }}});
            }
        }
        close() {
            this.readyState = 3;
            setTimeout(() => this.onclose({code: 1000}), 0);
        }
    };
}"""


def mocked_market(reply):
    market = wina.WinamaxMyMatch(MagicMock(), URL)
    market.bet_id, market.status = 70, "PREMATCH"
    market.page.evaluate.return_value = reply
    return market


def reply(result, **state):
    return {"result": result, "status": "PREMATCH", "available": True, **state}


class QuoteValidationTests(unittest.TestCase):
    def test_requested_outcomes_must_all_be_priced(self):
        market = mocked_market(reply({"mymatchOdds": 2.8,
                                      "subOutcomesOdds": {"1": 1.58, "2": 1.78}}))
        self.assertEqual(market.quote([1, 2]), 2.8)
        # Un doublon renvoie la cote simple : ce n'est pas le combo demande.
        for covered in ({"1": 1.68}, {"1": 1.5, "2": 1.5, "4": 1.5}):
            market.page.evaluate.return_value = reply({"mymatchOdds": 1.68, "subOutcomesOdds": covered})
            with self.subTest(covered=covered), self.assertRaisesRegex(ValueError, "incoherente"):
                market.quote([1, 2])

    def test_invalid_odds_are_rejected(self):
        for odds in (None, True, "2.5", 1, 0, math.nan, math.inf):
            market = mocked_market(reply({"mymatchOdds": odds,
                                          "subOutcomesOdds": {"1": 1.5, "2": 1.5}}))
            with self.subTest(odds=odds), self.assertRaisesRegex(ValueError, "invalide"):
                market.quote([1, 2])

    def test_explicit_refusal_transport_errors_and_closed_market(self):
        cases = [
            (reply({"mymatchError": {"errorId": -7}}), wina.MyMatchRefused, "-7"),
            ({"error": "aucune reponse GetOdds"}, wina.WinamaxSocketError, "aucune reponse"),
            (reply({}, available=False), wina.WinamaxMarketClosed, "disponible=False"),
            (reply({}, status="LIVE"), wina.WinamaxMarketClosed, "PREMATCH -> LIVE"),
        ]
        for payload, error, message in cases:
            with self.subTest(error=error.__name__), self.assertRaisesRegex(error, message):
                mocked_market(payload).quote([1, 2])
        market = mocked_market(None)
        market.page.evaluate.side_effect = PlaywrightError("Target closed")
        with self.assertRaisesRegex(wina.WinamaxSocketError, "Target closed"):
            market.quote([1, 2])

    def test_url_must_identify_the_match(self):
        self.assertEqual(wina.WinamaxMyMatch(MagicMock(), URL).match_id, 7)
        with self.assertRaisesRegex(ValueError, "URL"):
            wina.WinamaxMyMatch(MagicMock(), "https://www.winamax.fr/paris-sportifs")

    def test_duplicate_market_title_is_not_guessed(self):
        market = mocked_market({"status": "PREMATCH", "bets": [
            {"betId": 70, "available": True, "outcomes": []},
            {"betId": 72, "available": True, "outcomes": []},
        ]})
        with self.assertRaisesRegex(ValueError, "Plusieurs"):
            market.read_players()


class LocalSocketTests(unittest.TestCase):
    """Necessite Chromium Playwright ; faux socket, aucune connexion a Winamax."""

    def setUp(self):
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        browser = self.playwright.chromium.launch()
        self.addCleanup(browser.close)
        self.page = browser.new_page()
        self.page.evaluate(FAKE_SOCKET, MATCH_PAYLOAD)
        self.market = wina.WinamaxMyMatch(self.page, URL)
        self.market.socket_url = SOCKET

    def sent(self):
        return [json.loads(text[2:])[1] for text in self.page.evaluate("window.__sent")
                if text.startswith("42")]

    def test_reads_only_the_decisive_player_market(self):
        self.market.connect()
        players = self.market.read_players()
        self.assertEqual(self.page.evaluate("window.__urls"),
                         ["wss://sports.test/socket.io/?EIO=3&transport=websocket"])
        self.assertEqual(self.sent(), [{"route": "match:7"}])
        self.assertEqual([(p["Nom"], p["home"], p["outcome_id"]) for p in players], [
            ("Harry Kane", 1, 1), ("Luis Diaz", 1, 2), ("Kai Havertz", 0, 3), ("Bukayo Saka", 0, 4),
        ])
        self.assertEqual([p["Cote"] for p in players[:2]], [1.68, 1.94])
        # Selection suspendue et cote <= 1 : identite conservee, cote indisponible.
        self.assertTrue(all(math.isnan(p["Cote"]) for p in players[2:]))

    def test_quotes_each_combination_on_the_socket(self):
        self.market.connect()
        self.market.read_players()
        self.assertEqual(self.market.quote([1, 2]), 3.0)
        self.assertEqual(self.market.quote([1, 2, 4]), 4.0)
        requests = [message["myMatch"] for message in self.sent() if "myMatch" in message]
        self.assertEqual([r["params"] for r in requests], [
            {"matchId": 7, "outcomes": [{"betId": 70, "outcomeId": 1}, {"betId": 70, "outcomeId": 2}]},
            {"matchId": 7, "outcomes": [{"betId": 70, "outcomeId": i} for i in (1, 2, 4)]},
        ])
        self.assertEqual({r["method"] for r in requests}, {"GetOdds"})
        self.assertRegex(requests[0]["id"], r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        self.assertNotEqual(requests[0]["id"], requests[1]["id"])

    def test_kickoff_update_closes_the_market(self):
        self.market.connect()
        self.market.read_players()
        self.page.evaluate("window.__socket.push({matches: {7: {status: 'LIVE'}}})")
        self.page.wait_for_timeout(50)
        with self.assertRaisesRegex(wina.WinamaxMarketClosed, "LIVE"):
            self.market.quote([1, 2])

    def test_closed_socket_fails_then_reconnect_restores_quotes(self):
        self.market.connect()
        self.market.read_players()
        self.page.evaluate("window.__socket.close()")
        self.page.wait_for_timeout(50)
        with self.assertRaisesRegex(wina.WinamaxSocketError, "ferme"):
            self.market.quote([1, 2])
        self.market.reconnect()
        self.assertEqual(self.market.quote([1, 2]), 3.0)
        self.assertEqual(len(self.page.evaluate("window.__urls")), 2)

    def test_missing_market_returns_none(self):
        self.page.evaluate("window.__payload.bets = {}")
        self.market.connect()
        self.assertIsNone(self.market.read_players(timeout=300))


if __name__ == "__main__":
    unittest.main()
