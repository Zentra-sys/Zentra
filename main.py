
import random
import asyncio
import uuid
from collections import Counter
from itertools import combinations

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="ZENTRA ONLINE")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

rooms = {}
connections = {}
locks = {}

GAMES = {"ttt", "mafia", "durak", "poker"}

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["6", "7", "8", "9", "10", "J", "Q", "K", "A"]
VALUES = {r: i for i, r in enumerate(RANKS)}


def new_code():
    while True:
        code = str(uuid.uuid4())[:6].upper()
        if code not in rooms:
            return code


def player_names(room):
    return [p["name"] for p in room["players"]]


def find_player(room, name):
    return next((p for p in room["players"] if p["name"] == name), None)


def public_state(room):
    game = room["game"]
    state = room["state"]

    result = {
        "code": room["code"],
        "game": game,
        "players": player_names(room),
        "host": room["host"],
        "started": room["started"],
        "state": {},
    }

    if game == "ttt":
        result["state"] = state.copy()

    elif game == "durak":
        result["state"] = {
            "phase": state["phase"],
            "attacker": state["attacker"],
            "defender": state["defender"],
            "table": state["table"],
            "turn": state["turn"],
            "trump": state["trump"],
            "deck_count": len(state["deck"]),
            "hands": {
                p["name"]: len(state["hands"].get(p["name"], []))
                for p in room["players"]
            },
            "winner": state.get("winner"),
        }

    elif game == "mafia":
        result["state"] = {
            "phase": state["phase"],
            "day": state["day"],
            "players": [
                {
                    "name": p["name"],
                    "alive": p["alive"],
                    "role": p["role"] if state["phase"] == "finished" else None,
                }
                for p in state["players"]
            ],
            "votes": state.get("votes", {}),
            "message": state.get("message", ""),
            "winner": state.get("winner"),
        }

    elif game == "poker":
        result["state"] = {
            "phase": state["phase"],
            "community": state["community"],
            "pot": state["pot"],
            "turn": state["turn"],
            "dealer": state["dealer"],
            "players": [
                {
                    "name": p["name"],
                    "chips": p["chips"],
                    "bet": p["bet"],
                    "folded": p["folded"],
                    "cards": p["cards"] if state["phase"] == "finished"
                    or p["name"] == room.get("viewer") else [],
                }
                for p in state["players"]
            ],
            "winner": state.get("winner"),
        }

    return result


async def broadcast(code):
    room = rooms.get(code)
    if not room:
        return

    data = public_state(room)
    dead = []

    for ws in connections.get(code, []):
        try:
            await ws.send_json({"type": "state", **data})
        except Exception:
            dead.append(ws)

    for ws in dead:
        connections[code].remove(ws)


def card_value(card):
    return VALUES[card[:-1]]


def make_deck():
    deck = [r + s for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


def start_durak(room):
    players = room["players"]
    if not 2 <= len(players) <= 6:
        raise ValueError("Для Дурня потрібно від 2 до 6 гравців")

    deck = make_deck()
    hands = {p["name"]: [] for p in players}

    for _ in range(6):
        for p in players:
            hands[p["name"]].append(deck.pop())

    trump = deck[-1][-1] if deck else "♠"

    room["state"] = {
        "phase": "playing",
        "deck": deck,
        "hands": hands,
        "trump": trump,
        "table": [],
        "attacker": players[0]["name"],
        "defender": players[1]["name"],
        "turn": players[0]["name"],
        "winner": None,
    }


def start_mafia(room):
    players = room["players"]
    n = len(players)

    if n < 4:
        raise ValueError("Для Мафії потрібно щонайменше 4 гравці")

    roles = ["mafia", "doctor", "detective"]
    roles += ["citizen"] * (n - len(roles))
    random.shuffle(roles)

    room["state"] = {
        "phase": "night",
        "day": 1,
        "players": [
            {"name": p["name"], "role": roles[i], "alive": True}
            for i, p in enumerate(players)
        ],
        "night_actions": {},
        "votes": {},
        "message": "Настала ніч. Таємно оберіть ціль.",
        "winner": None,
    }


def start_poker(room):
    players = room["players"]
    if not 2 <= len(players) <= 6:
        raise ValueError("Для Покеру потрібно від 2 до 6 гравців")

    for p in players:
        p["chips"] = 1000

    room["state"] = {
        "phase": "playing",
        "deck": make_deck(),
        "community": [],
        "pot": 0,
        "turn": players[0]["name"],
        "dealer": players[0]["name"],
        "players": [],
        "winner": None,
        "current_bet": 0,
        "last_action": {},
    }

    state = room["state"]
    for p in players:
        state["players"].append({
            "name": p["name"],
            "cards": [state["deck"].pop(), state["deck"].pop()],
            "chips": 1000,
            "bet": 0,
            "folded": False,
            "acted": False,
        })

    state["community"] = [state["deck"].pop() for _ in range(3)]


def poker_rank(cards):
    values = sorted([card_value(c) for c in cards], reverse=True)
    suits = [c[-1] for c in cards]
    counts = Counter(values)
    groups = sorted(
        [(count, value) for value, count in counts.items()],
        reverse=True
    )

    flush = len(set(suits)) == 1
    unique = sorted(set(values), reverse=True)
    straight = None

    if len(unique) >= 5:
        for i in range(len(unique) - 4):
            seq = unique[i:i + 5]
            if seq[0] - seq[4] == 4:
                straight = seq[0]
                break

    if set([12, 0, 1, 2, 3]).issubset(set(values)):
        straight = max(straight or 0, 3)

    if flush and straight is not None:
        return (8, straight)
    if groups[0][0] == 4:
        return (7, groups[0][1], groups[1][1])
    if groups[0][0] == 3 and groups[1][0] == 2:
        return (6, groups[0][1], groups[1][1])
    if flush:
        return (5, *values[:5])
    if straight is not None:
        return (4, straight)
    if groups[0][0] == 3:
        kickers = [v for v in values if v != groups[0][1]]
        return (3, groups[0][1], *kickers[:2])
    pairs = [v for count, v in groups if count == 2]
    if len(pairs) >= 2:
        kicker = max(v for v in values if v not in pairs)
        return (2, pairs[0], pairs[1], kicker)
    if len(pairs) == 1:
        kickers = [v for v in values if v != pairs[0]]
        return (1, pairs[0], *kickers[:3])
    return (0, *values[:5])


def finish_poker(room):
    state = room["state"]
    active = [p for p in state["players"] if not p["folded"]]

    if not active:
        return

    if len(active) == 1:
        winner = active[0]
    else:
        winner = max(
            active,
            key=lambda p: poker_rank(p["cards"] + state["community"])
        )

    winner["chips"] += state["pot"]
    state["winner"] = winner["name"]
    state["phase"] = "finished"


def apply_durak(room, name, action):
    state = room["state"]
    if state["phase"] != "playing":
        raise ValueError("Гра не триває")

    hands = state["hands"]
    if name not in hands:
        raise ValueError("Гравця не знайдено")

    kind = action.get("action")
    card = action.get("card")

    if kind == "attack":
        if name != state["attacker"] and state["table"]:
            raise ValueError("Зараз не твій хід")
        if not card or card not in hands[name]:
            raise ValueError("Такої карти немає")
        if len(state["table"]) >= 6:
            raise ValueError("На столі вже достатньо карт")
        if state["table"] and card_value(card) not in [
            card_value(x["attack"]) for x in state["table"]
        ]:
            raise ValueError("Можна підкидати лише карту того самого значення")

        hands[name].remove(card)
        state["table"].append({"attack": card, "defense": None})
        state["turn"] = state["defender"]

    elif kind == "defend":
        if name != state["defender"]:
            raise ValueError("Зараз відбивається інший гравець")
        if not card or card not in hands[name]:
            raise ValueError("Такої карти немає")
        if not state["table"]:
            raise ValueError("Немає карти для відбиття")

        target = next(
            (x for x in state["table"] if x["defense"] is None), None
        )
        if not target:
            raise ValueError("Усі карти вже відбиті")

        attack = target["attack"]
        beats = (
            card[-1] == attack[-1] and card_value(card) > card_value(attack)
        ) or (
            card[-1] == state["trump"] and attack[-1] != state["trump"]
        )

        if not beats:
            raise ValueError("Ця карта не б'є атакуючу")

        hands[name].remove(card)
        target["defense"] = card
        state["turn"] = state["attacker"]

    elif kind == "take":
        if name != state["defender"]:
            raise ValueError("Не твій хід")
        for item in state["table"]:
            hands[name].append(item["attack"])
            if item["defense"]:
                hands[name].append(item["defense"])
        state["table"] = []
        state["attacker"] = name
        names = player_names(room)
        idx = names.index(name)
        state["defender"] = names[(idx + 1) % len(names)]
        state["turn"] = state["attacker"]

    elif kind == "pass":
        if name != state["attacker"]:
            raise ValueError("Не твій хід")
        if not state["table"] or any(x["defense"] is None for x in state["table"]):
            raise ValueError("Спочатку потрібно відбити всі карти")

        state["table"] = []
        names = player_names(room)
        idx = names.index(state["defender"])
        state["attacker"] = state["defender"]
        state["defender"] = names[(idx + 1) % len(names)]
        state["turn"] = state["attacker"]

    else:
        raise ValueError("Невідома дія")

    # Добір карт до шести
    for p in room["players"]:
        hand = hands[p["name"]]
        while len(hand) < 6 and state["deck"]:
            hand.append(state["deck"].pop())

    # Перемога, коли колода порожня і рука спорожніла
    if not state["deck"]:
        empty = [n for n, h in hands.items() if not h]
        if empty:
            state["winner"] = empty[0]
            state["phase"] = "finished"


def apply_mafia(room, name, action):
    state = room["state"]
    players = state["players"]
    me = next((p for p in players if p["name"] == name), None)

    if not me or not me["alive"]:
        raise ValueError("Ти не можеш діяти")

    kind = action.get("action")
    target = action.get("target")
    target_player = next((p for p in players if p["name"] == target), None)

    if not target_player or not target_player["alive"]:
        raise ValueError("Невірна ціль")

    if state["phase"] == "night":
        if kind != "night":
            raise ValueError("Зараз ніч")

        if me["role"] not in ("mafia", "doctor", "detective"):
            raise ValueError("Твоя роль не має нічної дії")

        if me["role"] == "mafia" and target_player["role"] == "mafia":
            raise ValueError("Не можна обрати іншого мафіозі")

        state["night_actions"][name] = {
            "role": me["role"],
            "target": target
        }

        active_special = [
            p for p in players
            if p["alive"] and p["role"] in ("mafia", "doctor", "detective")
        ]

        if all(p["name"] in state["night_actions"] for p in active_special):
            actions = list(state["night_actions"].values())
            mafia_target = next(
                (a["target"] for a in actions if a["role"] == "mafia"), None
            )
            doctor_target = next(
                (a["target"] for a in actions if a["role"] == "doctor"), None
            )

            if mafia_target and mafia_target != doctor_target:
                victim = next(p for p in players if p["name"] == mafia_target)
                victim["alive"] = False
                state["message"] = f"Вночі вибув гравець: {mafia_target}"
            else:
                state["message"] = "Цієї ночі ніхто не вибув"

            state["phase"] = "day"
            state["votes"] = {}
            state["night_actions"] = {}
            state["day"] += 1

    elif state["phase"] == "day":
        if kind != "vote":
            raise ValueError("Зараз голосування")
        if target == name:
            raise ValueError("Не можна голосувати за себе")

        state["votes"][name] = target
        alive = [p for p in players if p["alive"]]

        if all(p["name"] in state["votes"] for p in alive):
            tally = Counter(state["votes"].values())
            eliminated, count = tally.most_common(1)[0]

            if sum(1 for v in tally.values() if v == count) == 1:
                out = next(p for p in players if p["name"] == eliminated)
                out["alive"] = False
                state["message"] = f"Голосування: вибув {eliminated}"
            else:
                state["message"] = "Голосування завершилося нічиєю"

            state["votes"] = {}
            state["phase"] = "night"

    mafia_alive = sum(1 for p in players if p["alive"] and p["role"] == "mafia")
    citizens_alive = sum(1 for p in players if p["alive"] and p["role"] != "mafia")

    if mafia_alive == 0:
        state["winner"] = "Мирні жителі"
        state["phase"] = "finished"
    elif mafia_alive >= citizens_alive:
        state["winner"] = "Мафія"
        state["phase"] = "finished"


def apply_poker(room, name, action):
    state = room["state"]
    if state["phase"] != "playing":
        raise ValueError("Роздача завершена")

    players = state["players"]
    me = next((p for p in players if p["name"] == name), None)

    if not me or me["folded"]:
        raise ValueError("Ти не можеш діяти")
    if state["turn"] != name:
        raise ValueError("Зараз не твій хід")

    kind = action.get("action")

    if kind == "fold":
        me["folded"] = True

    elif kind == "check":
        if me["bet"] != state["current_bet"]:
            raise ValueError("Потрібно зрівняти ставку або скинути карти")

    elif kind == "call":
        amount = state["current_bet"] - me["bet"]
        amount = min(amount, me["chips"])
        me["chips"] -= amount
        me["bet"] += amount
        state["pot"] += amount

    elif kind == "raise":
        amount = int(action.get("amount", 0))
        if amount <= 0:
            raise ValueError("Некоректна ставка")
        needed = state["current_bet"] - me["bet"] + amount
        if needed > me["chips"]:
            raise ValueError("Недостатньо фішок")
        me["chips"] -= needed
        me["bet"] += needed
        state["pot"] += needed
        state["current_bet"] = me["bet"]
        for p in players:
            if p["name"] != name and not p["folded"]:
                p["acted"] = False
    else:
        raise ValueError("Невідома дія")

    me["acted"] = True
    active = [p for p in players if not p["folded"]]

    if len(active) == 1:
        finish_poker(room)
        return

    if all(p["acted"] and p["bet"] == state["current_bet"] for p in active):
        if len(state["community"]) < 5:
            state["community"].append(state["deck"].pop())
            if len(state["community"]) < 5:
                state["community"].append(state["deck"].pop())
            for p in active:
                p["bet"] = 0
                p["acted"] = False
            state["current_bet"] = 0
        else:
            finish_poker(room)
            return

    idx = next(i for i, p in enumerate(players) if p["name"] == name)
    for step in range(1, len(players) + 1):
        candidate = players[(idx + step) % len(players)]
        if not candidate["folded"]:
            state["turn"] = candidate["name"]
            break


def apply_action(room, name, action):
    if not room["started"]:
        raise ValueError("Гра ще не почалася")

    game = room["game"]

    if game == "ttt":
        state = room["state"]
        if state.get("winner") or state.get("draw"):
            raise ValueError("Гра завершена")
        if name != state["turn"]:
            raise ValueError("Зараз не твій хід")

        index = action.get("index")
        if not isinstance(index, int) or not 0 <= index < 9:
            raise ValueError("Невірна клітинка")
        if state["board"][index]:
            raise ValueError("Клітинка зайнята")

        mark = state["marks"][name]
        state["board"][index] = mark

        wins = [
            [0,1,2],[3,4,5],[6,7,8],
            [0,3,6],[1,4,7],[2,5,8],
            [0,4,8],[2,4,6]
        ]

        if any(all(state["board"][i] == mark for i in line) for line in wins):
            state["winner"] = name
        elif all(state["board"]):
            state["draw"] = True
        else:
            other = next(n for n in state["marks"] if n != name)
            state["turn"] = other

    elif game == "durak":
        apply_durak(room, name, action)
    elif game == "mafia":
        apply_mafia(room, name, action)
    elif game == "poker":
        apply_poker(room, name, action)


@app.get("/")
def home():
    return {"name": "ZENTRA", "status": "online", "realtime": True}


@app.get("/health")
def health():
    return {"ok": True, "rooms": len(rooms)}


@app.get("/api/rooms")
def list_rooms():
    return [
        {
            "code": r["code"],
            "game": r["game"],
            "players": len(r["players"]),
            "started": r["started"],
        }
        for r in rooms.values()
    ]


@app.post("/api/rooms")
async def create_room(data: dict):
    game = data.get("game")
    name = str(data.get("player_name", "")).strip()[:20]

    if game not in GAMES:
        raise HTTPException(400, "Невідома гра")
    if not name:
        raise HTTPException(400, "Введи ім'я")

    code = new_code()
    rooms[code] = {
        "code": code,
        "game": game,
        "host": name,
        "players": [{"name": name}],
        "started": False,
        "state": {},
    }
    locks[code] = asyncio.Lock()
    return {"code": code, "game": game}


@app.post("/api/rooms/join")
async def join_room(data: dict):
    code = str(data.get("code", "")).strip().upper()
    name = str(data.get("player_name", "")).strip()[:20]

    room = rooms.get(code)
    if not room:
        raise HTTPException(404, "Кімнату не знайдено")
    if not name:
        raise HTTPException(400, "Введи ім'я")
    if room["started"]:
        raise HTTPException(400, "Гра вже почалася")
    if find_player(room, name):
        return {"ok": True, "code": code}

    limits = {"ttt": 2, "mafia": 12, "durak": 6, "poker": 6}
    if len(room["players"]) >= limits[room["game"]]:
        raise HTTPException(400, "Кімната заповнена")

    room["players"].append({"name": name})
    await broadcast(code)
    return {"ok": True, "code": code}


@app.get("/api/rooms/{code}")
def get_room(code: str):
    room = rooms.get(code.upper())
    if not room:
        raise HTTPException(404, "Кімнату не знайдено")
    return public_state(room)


@app.post("/api/rooms/{code}/start")
async def start_room(code: str, data: dict):
    code = code.upper()
    room = rooms.get(code)
    if not room:
        raise HTTPException(404, "Кімнату не знайдено")

    name = str(data.get("player_name", ""))
    if name != room["host"]:
        raise HTTPException(403, "Почати гру може тільки творець кімнати")
    if room["started"]:
        raise HTTPException(400, "Гра вже почалася")

    try:
        if room["game"] == "ttt":
            if len(room["players"]) != 2:
                raise ValueError("Для хрестиків-нуликів потрібно 2 гравці")
            names = player_names(room)
            room["state"] = {
                "board": [""] * 9,
                "turn": names[0],
                "marks": {names[0]: "X", names[1]: "O"},
                "winner": None,
                "draw": False,
            }
        elif room["game"] == "durak":
            start_durak(room)
        elif room["game"] == "mafia":
            start_mafia(room)
        elif room["game"] == "poker":
            start_poker(room)

        room["started"] = True
        await broadcast(code)
        return {"ok": True, "state": public_state(room)}

    except ValueError as e:
        raise HTTPException(400, str(e))


@app.websocket("/ws/{code}")
async def websocket_endpoint(ws: WebSocket, code: str, name: str = ""):
    code = code.upper()
    room = rooms.get(code)

    if not room or not find_player(room, name):
        await ws.close(code=1008)
        return

    await ws.accept()
    connections.setdefault(code, []).append(ws)

    try:
        await ws.send_json({"type": "state", **public_state(room)})

        while True:
            action = await ws.receive_json()
            async with locks[code]:
                try:
                    apply_action(room, name, action)
                    await broadcast(code)
                except ValueError as e:
                    await ws.send_json({"type": "error", "message": str(e)})

    except WebSocketDisconnect:
        if ws in connections.get(code, []):
            connections[code].remove(ws)
    except Exception:
        if ws in connections.get(code, []):
            connections[code].remove(ws)
