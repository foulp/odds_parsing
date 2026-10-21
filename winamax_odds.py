"""Cotes Winamax Joueur decisif et MyMatch via le socket.io du site.

La page du match fournit le contexte navigateur (origine, cookies) et l'adresse
du socket. Une connexion dediee, ouverte depuis cette page, lit le marche puis
demande chaque combinaison avec GetOdds, sans clic ni lecture du panier.
Une reponse doit couvrir exactement les selections demandees.
"""

import logging
import math
import re

from playwright.sync_api import Error as PlaywrightError


MARKET_TITLE = "Joueur décisif"
TEAMS = {1: 1, 2: 0}  # equipe Winamax -> colonne home


class WinamaxSocketError(RuntimeError):
    """Socket ferme ou reponse absente : le combo peut etre retente apres reconnexion."""


class WinamaxMarketClosed(RuntimeError):
    """Marche suspendu ou rencontre plus en avant-match."""


class MyMatchRefused(ValueError):
    """Winamax refuse explicitement cette combinaison."""


CONNECT_JS = """url => new Promise((resolve, reject) => {
    const previous = window.__winaMyMatch;
    if (previous) { clearInterval(previous.ping); previous.ws.close(); }
    const socketUrl = new URL(url);
    socketUrl.searchParams.delete('sid');
    socketUrl.searchParams.set('transport', 'websocket');
    const ws = new WebSocket(socketUrl.href);
    const state = {ws, pending: new Map(), closed: null,
                   data: {matches: {}, bets: {}, outcomes: {}, odds: {}}};
    window.__winaMyMatch = state;
    const timer = setTimeout(() => { reject(new Error('connexion expiree')); ws.close(); }, 15000);
    ws.onmessage = event => {
        const text = String(event.data);
        if (text.startsWith('0')) {
            const info = JSON.parse(text.slice(1));
            state.ping = setInterval(() => ws.readyState === 1 && ws.send('2'),
                                     info.pingInterval || 25000);
            return;
        }
        if (text === '40') { clearTimeout(timer); resolve(); return; }
        if (!text.startsWith('42')) return;
        const payload = JSON.parse(text.slice(2))[1] || {};
        for (const key of Object.keys(state.data)) {
            for (const [id, value] of Object.entries(payload[key] || {})) {
                state.data[key][id] = value && typeof value === 'object'
                    ? Object.assign(state.data[key][id] || {}, value) : value;
            }
        }
        const result = payload.myMatchResults;
        if (result && state.pending.has(result.id)) state.pending.get(result.id)(result);
    };
    ws.onclose = event => {
        state.closed = `socket ferme (code ${event.code})`;
        clearInterval(state.ping);
        clearTimeout(timer);
        reject(new Error(state.closed));
        for (const done of state.pending.values()) done({error: state.closed});
    };
})"""

MARKET_JS = """async ({matchId, title, timeout}) => {
    const state = window.__winaMyMatch;
    state.ws.send('42' + JSON.stringify(['m', {route: 'match:' + matchId}]));
    const deadline = performance.now() + timeout;
    while (performance.now() < deadline && !state.closed) {
        const match = state.data.matches[matchId];
        const bets = Object.values(state.data.bets).filter(
            bet => bet.matchId === matchId && bet.betTitle === title);
        if (match && bets.length && bets.every(
                bet => (bet.outcomes || []).every(id => state.data.outcomes[id]))) {
            return {status: match.status, bets: bets.map(bet => ({
                betId: bet.betId, available: bet.available,
                outcomes: bet.outcomes.map(id => ({
                    id, ...state.data.outcomes[id], odds: state.data.odds[id] ?? null,
                })),
            }))};
        }
        await new Promise(done => setTimeout(done, 100));
    }
    if (state.closed) throw new Error(state.closed);
    return null;
}"""

QUOTE_JS = """({matchId, betId, outcomes, timeout}) => new Promise(resolve => {
    const state = window.__winaMyMatch;
    if (!state || state.closed || state.ws.readyState !== 1) {
        resolve({error: state?.closed || 'socket non ouvert'});
        return;
    }
    // Meme format UUID v4 que le site ; getRandomValues reste disponible hors HTTPS.
    const id = '10000000-1000-4000-8000-100000000000'.replace(/[018]/g, c =>
        (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
    const timer = setTimeout(() => {
        state.pending.delete(id);
        resolve({error: 'aucune reponse GetOdds'});
    }, timeout);
    state.pending.set(id, reply => {
        clearTimeout(timer);
        state.pending.delete(id);
        resolve({...reply, status: state.data.matches[matchId]?.status,
                 available: state.data.bets[betId]?.available});
    });
    state.ws.send('42' + JSON.stringify(['m', {myMatch: {
        id, method: 'GetOdds',
        params: {matchId, outcomes: outcomes.map(outcomeId => ({betId, outcomeId}))},
    }}]));
})"""


def valid_odds(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 1)


class WinamaxMyMatch:
    def __init__(self, page, url):
        match = re.search(r"/match/(\d+)", url)
        if not match:
            raise ValueError(f"URL de match Winamax invalide : {url}")
        self.page, self.url, self.match_id = page, url, int(match[1])
        self.socket_url = self.bet_id = self.status = None

    def open(self, timeout=20000):
        """Charge la page du match et reutilise l'adresse de son socket.io."""
        with self.page.expect_websocket(lambda ws: "/socket.io/" in ws.url,
                                        timeout=timeout) as socket:
            self.page.goto(self.url, wait_until="domcontentloaded")
        self.socket_url = socket.value.url
        self.connect()

    def connect(self):
        try:
            self.page.evaluate(CONNECT_JS, self.socket_url)
        except PlaywrightError as exc:
            raise WinamaxSocketError(f"Connexion socket Winamax impossible : {exc}") from exc

    def read_players(self, timeout=15000):
        """Simples du marche, ou None s'il n'est pas propose ; cote NaN si indisponible."""
        try:
            market = self.page.evaluate(MARKET_JS, {
                "matchId": self.match_id, "title": MARKET_TITLE, "timeout": timeout,
            })
        except PlaywrightError as exc:
            raise WinamaxSocketError(f"Lecture du marche Winamax impossible : {exc}") from exc
        if market is None:
            return None
        if len(market["bets"]) != 1:
            raise ValueError(f"Plusieurs marches Winamax {MARKET_TITLE} : "
                             f"{[bet['betId'] for bet in market['bets']]}")
        bet = market["bets"][0]
        if self.bet_id is not None and bet["betId"] != self.bet_id:
            raise ValueError(f"Marche Winamax remplace : {self.bet_id} -> {bet['betId']}")
        self.bet_id, self.status = bet["betId"], market["status"]
        players = []
        for outcome in bet["outcomes"]:
            name = " ".join(str(outcome.get("label") or "").split())
            if not name or outcome.get("team") not in TEAMS:
                raise ValueError(f"Selection Winamax inattendue : {outcome}")
            odds = outcome.get("odds")
            if not (bet["available"] and outcome.get("available") and valid_odds(odds)):
                logging.debug("[WINA] Cote indisponible/invalide pour %s : %r", name, odds)
                odds = math.nan
            players.append({"home": TEAMS[outcome["team"]], "Nom": name,
                            "Cote": float(odds), "outcome_id": outcome["id"]})
        return players

    def reconnect(self):
        """Nouvelle connexion et relecture du marche, sans recharger la page."""
        self.connect()
        if self.read_players() is None:
            raise WinamaxMarketClosed(f"Marche {MARKET_TITLE} disparu de Winamax")

    def quote(self, outcome_ids, timeout=10000):
        try:
            reply = self.page.evaluate(QUOTE_JS, {
                "matchId": self.match_id, "betId": self.bet_id,
                "outcomes": list(outcome_ids), "timeout": timeout,
            })
        except PlaywrightError as exc:
            raise WinamaxSocketError(f"GetOdds impossible : {exc}") from exc
        if reply.get("error"):
            raise WinamaxSocketError(reply["error"])
        if reply.get("available") is False or reply.get("status") != self.status:
            raise WinamaxMarketClosed(
                f"Marche {MARKET_TITLE} ferme sur Winamax (statut {self.status} -> "
                f"{reply.get('status')}, disponible={reply.get('available')})"
            )
        result = reply.get("result") or {}
        if "mymatchError" in result:
            raise MyMatchRefused(f"MyMatch refuse par Winamax : {result['mymatchError']}")
        odds = result.get("mymatchOdds")
        covered = set(result.get("subOutcomesOdds") or {})
        if covered != {str(outcome) for outcome in outcome_ids}:
            raise ValueError(f"Reponse MyMatch incoherente : {sorted(covered)} pour {list(outcome_ids)}")
        if not valid_odds(odds):
            raise ValueError(f"Cote MyMatch invalide : {odds!r}")
        return float(odds)
