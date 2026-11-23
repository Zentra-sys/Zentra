import os
import random
import sqlite3
import json
import urllib.request
import urllib.error

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="ZENTRA API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB = os.getenv("ZENTRA_DB", "zentra.db")
rooms = {}
connections = {}


# =========================
# DATABASE
# =========================

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                uid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                premium INTEGER DEFAULT 0
            )
        """)


init_db()

with db() as con:
    columns = [row["name"] for row in con.execute("PRAGMA table_info(users)").fetchall()]
    if "premium" not in columns:
        con.execute("ALTER TABLE users ADD COLUMN premium INTEGER DEFAULT 0")


# =========================
# MODELS
# =========================

class Profile(BaseModel):
    uid: str
    name: str


class RoomCreate(BaseModel):
    name: str
    game: str = "ttt"
    open: bool = False


class JoinRoom(BaseModel):
    code: str
    name: str


# =========================
# BASIC
# =========================

@app.get("/")
def home():
    return {"name": "ZENTRA", "status": "online"}


@app.get("/health")
def health():
    return {
        "ok": True,
        "rooms": len(rooms)
    }


# =========================
# PROFILES
# =========================

@app.post("/api/profile")
def profile(data: Profile):
    uid = data.uid.strip()
    name = data.name.strip()[:24]

    if not uid or not name:
        raise HTTPException(400, "Вкажи нікнейм")

    with db() as con:
        existing = con.execute(
            """
            SELECT uid FROM users
            WHERE lower(name)=lower(?) AND uid!=?
            """,
            (name, uid)
        ).fetchone()

        if existing:
            raise HTTPException(
                409,
                "Цей нікнейм уже зайнятий! Обери інший."
            )

        con.execute(
            """
            INSERT INTO users(uid, name)
            VALUES(?, ?)
            ON CONFLICT(uid)
            DO UPDATE SET name=excluded.name
            """,
            (uid, name)
        )

        user = con.execute(
            "SELECT * FROM users WHERE uid=?",
            (uid,)
        ).fetchone()

    return {
        "uid": user["uid"],
        "name": user["name"],
        "premium": bool(user["premium"])
    }


@app.get("/api/profile/{uid}")
def get_profile(uid: str):
    with db() as con:
        user = con.execute(
            "SELECT * FROM users WHERE uid=?",
            (uid,)
        ).fetchone()

    if not user:
        raise HTTPException(404, "Профіль не знайдено")

    return {
        "uid": user["uid"],
        "name": user["name"],
        "premium": bool(user["premium"])
    }


# =========================
# ZENTRA PREMIUM — TELEGRAM STARS
# =========================

PREMIUM_PRICE = 20


def telegram_api(method, payload):
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token:
        raise HTTPException(
            503,
            "На Render не налаштовано TELEGRAM_BOT_TOKEN"
        )

    url = f"https://api.telegram.org/bot{token}/{method}"

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))

    except Exception as error:
        raise HTTPException(
            502,
            f"Помилка Telegram API: {error}"
        )

    if not result.get("ok"):
        raise HTTPException(
            502,
            result.get("description", "Telegram API error")
        )

    return result.get("result")


@app.post("/api/premium/invoice")
def premium_invoice(data: dict):
    uid = str(data.get("uid", "")).strip()

    if not uid:
        raise HTTPException(400, "Не передано uid")

    with db() as con:
        user = con.execute(
            "SELECT uid, premium FROM users WHERE uid=?",
            (uid,)
        ).fetchone()

    if not user:
        raise HTTPException(404, "Профіль не знайдено")

    if user["premium"]:
        return {
            "premium": True,
            "already_owned": True
        }

    link = telegram_api("createInvoiceLink", {
        "title": "ZENTRA PREMIUM",
        "description": "Постійний доступ до преміум-ігор ZENTRA",
        "payload": f"zentra_premium:{uid}",
        "currency": "XTR",
        "prices": [{
            "label": "ZENTRA PREMIUM lifetime",
            "amount": PREMIUM_PRICE
        }],
    })

    return {
        "premium": False,
        "invoice_link": link,
        "price": PREMIUM_PRICE,
        "currency": "XTR"
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    supplied = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        ""
    )

    if not expected or supplied != expected:
        raise HTTPException(403, "Invalid webhook secret")

    update = await request.json()

    checkout = update.get("pre_checkout_query")

    if checkout:
        payload = str(checkout.get("invoice_payload", ""))
        valid = False

        if payload.startswith("zentra_premium:"):
            uid = payload.split(":", 1)[1]

            with db() as con:
                valid = con.execute(
                    "SELECT 1 FROM users WHERE uid=?",
                    (uid,)
                ).fetchone() is not None

        telegram_api("answerPreCheckoutQuery", {
            "pre_checkout_query_id": checkout["id"],
            "ok": bool(valid),
            **(
                {}
                if valid
                else {"error_message": "Invalid ZENTRA purchase"}
            )
        })

        return {"ok": True}

    message = update.get("message") or {}
    payment = message.get("successful_payment")

    if payment:
        payload = str(payment.get("invoice_payload", ""))
        total = payment.get("total_amount")
        currency = payment.get("currency")

        if (
            payload.startswith("zentra_premium:")
            and total == PREMIUM_PRICE
            and currency == "XTR"
        ):
            uid = payload.split(":", 1)[1]
            charge = payment.get("telegram_payment_charge_id")

            if charge:
                with db() as con:
                    con.execute("""
                        CREATE TABLE IF NOT EXISTS payments (
                            charge_id TEXT PRIMARY KEY,
                            uid TEXT NOT NULL
                        )
                    """)

                    try:
                        con.execute(
                            "INSERT INTO payments(charge_id,uid) VALUES(?,?)",
                            (charge, uid)
                        )
                    except sqlite3.IntegrityError:
                        return {"ok": True}

                    con.execute(
                        "UPDATE users SET premium=1 WHERE uid=?",
                        (uid,)
                    )

    return {"ok": True}


# =========================
# ROOMS
# =========================

def new_code():
    code = str(random.randint(100000, 999999))

    while code in rooms:
        code = str(random.randint(100000, 999999))

    return code


def public_room(room):
    """Публічна інформація без секретних ролей."""
    result = {
        "code": room["code"],
        "game": room["game"],
        "host": room["host"],
        "players": room["players"],
        "open": room["open"],
        "started": room["started"]
    }

    if room["game"] == "mafia" and room["started"]:
        state = room["state"]

        result["state"] = {
            "phase": state["phase"],
            "round": state["round"],
            "alive": state["alive"],
            "winner": state["winner"],
            "last_result": state["last_result"],
            "eliminated": state["eliminated"],
            "votes_count": len(state["votes"]),
            "players_count": len(state["alive"])
        }

    elif room["game"] == "durak" and room["started"]:
        state = room["state"]

        result["state"] = {
            k: v
            for k, v in state.items()
            if k not in ("hands", "deck")
        }

        result["state"]["deck_count"] = len(state["deck"])
        result["hand_counts"] = {
            p: len(h)
            for p, h in state["hands"].items()
        }

    elif room["game"] == "poker" and room["started"]:
        state = room["state"]

        result["state"] = {
            k: v
            for k, v in state.items()
            if k not in ("hands", "deck")
        }

        result["hand_counts"] = {
            p: (
                len(h)
                if p in state["folded"]
                else 2
            )
            for p, h in state["hands"].items()
        }

    else:
        result["state"] = room["state"]

    return result


@app.post("/api/rooms")
def create_room(data: RoomCreate):
    game = data.game.lower()

    if game not in (
        "ttt",
        "durak",
        "mafia",
        "poker",
        "crocodile"
    ):
        raise HTTPException(400, "Невідомий тип гри")

    name = data.name.strip()[:24]

    if not name:
        raise HTTPException(400, "Вкажи ім'я")

    code = new_code()

    rooms[code] = {
        "code": code,
        "game": game,
        "host": name,
        "players": [name],
        "open": data.open,
        "started": False,
        "state": {}
    }

    return public_room(rooms[code])


@app.get("/api/rooms")
def get_rooms():
    return [
        public_room(room)
        for room in rooms.values()
    ]


@app.get("/api/lobbies")
def lobbies():
    return [
        public_room(room)
        for room in rooms.values()
        if room["open"] and not room["started"]
    ]


@app.post("/api/rooms/join")
def join_room(data: JoinRoom):
    code = data.code.strip()
    room = rooms.get(code)

    if not room:
        raise HTTPException(404, "Кімнату не знайдено")

    if room["started"]:
        raise HTTPException(400, "Гра вже почалася")

    name = data.name.strip()[:24]

    if not name:
        raise HTTPException(400, "Вкажи ім'я")

    if name.casefold() not in [
        p.casefold()
        for p in room["players"]
    ]:
        limits = {
            "ttt": 2,
            "mafia": 10,
            "crocodile": 4,
            "durak": 6,
            "poker": 6
        }

        limit = limits.get(room["game"], 10)

        if len(room["players"]) >= limit:
            raise HTTPException(
                400,
                f"У цій грі максимум {limit} гравців"
            )

        room["players"].append(name)

    return public_room(room)


@app.get("/api/rooms/{code}")
def room_info(code: str):
    room = rooms.get(code)

    if not room:
        raise HTTPException(404, "Кімнату не знайдено")

    return public_room(room)


# =========================
# TIC TAC TOE
# =========================

def start_ttt(room):
    room["started"] = True

    room["state"] = {
        "turn": 0,
        "board": [""] * 9,
        "winner": None
    }


# =========================
# MAFIA
# =========================

def mafia_roles(players):
    shuffled = players[:]
    random.shuffle(shuffled)

    count = len(shuffled)
    mafia_count = max(1, count // 4)

    roles = {}

    for name in shuffled[:mafia_count]:
        roles[name] = "mafia"

    remaining = shuffled[mafia_count:]

    if remaining:
        roles[remaining.pop()] = "doctor"

    if count >= 5 and remaining:
        roles[remaining.pop()] = "detective"

    for name in remaining:
        roles[name] = "citizen"

    return roles


def start_mafia(room):
    players = room["players"]

    if not 4 <= len(players) <= 10:
        raise HTTPException(
            400,
            "Для Мафії потрібно від 4 до 10 гравців"
        )

    roles = mafia_roles(players)

    room["started"] = True

    room["state"] = {
        "phase": "night",
        "round": 1,
        "alive": players[:],
        "roles": roles,
        "night_actions": {},
        "votes": {},
        "winner": None,
        "last_result": "Настала ніч. Оберіть свої дії.",
        "private_checks": {},
        "eliminated": None
    }


def mafia_finish(room):
    state = room["state"]
    alive = state["alive"]
    roles = state["roles"]

    mafia_alive = [
        name
        for name in alive
        if roles[name] == "mafia"
    ]

    town_alive = [
        name
        for name in alive
        if roles[name] != "mafia"
    ]

    if not mafia_alive:
        state["winner"] = "citizens"
        state["phase"] = "finished"
        state["last_result"] = "Мирні жителі перемогли!"

    elif len(mafia_alive) >= len(town_alive):
        state["winner"] = "mafia"
        state["phase"] = "finished"
        state["last_result"] = "Мафія перемогла!"

    if state["winner"]:
        return True

    return False


def resolve_night(room):
    state = room["state"]
    roles = state["roles"]
    alive = state["alive"]
    actions = state["night_actions"]

    mafia_votes = [
        action["target"]
        for name, action in actions.items()
        if (
            roles.get(name) == "mafia"
            and action.get("type") == "kill"
            and action.get("target") in alive
        )
    ]

    victim = None

    if mafia_votes:
        victim = max(
            set(mafia_votes),
            key=mafia_votes.count
        )

    doctor_target = next(
        (
            action["target"]
            for name, action in actions.items()
            if (
                roles.get(name) == "doctor"
                and action.get("type") == "save"
            )
        ),
        None
    )

    if victim and victim == doctor_target:
        state["last_result"] = (
            "Лікар врятував гравця. "
            "Ніхто не вибув цієї ночі."
        )

        eliminated = None

    elif victim:
        alive.remove(victim)
        eliminated = victim

        state["last_result"] = (
            f"Вночі вибув гравець: {victim}."
        )

    else:
        eliminated = None

        state["last_result"] = (
            "Цієї ночі ніхто не вибув."
        )

    state["eliminated"] = eliminated

    for name, action in actions.items():
        if (
            roles.get(name) == "detective"
            and action.get("type") == "check"
        ):
            target = action.get("target")

            if target in roles:
                state["private_checks"][name] = {
                    "target": target,
                    "is_mafia": roles[target] == "mafia"
                }

    state["night_actions"] = {}

    if mafia_finish(room):
        return

    state["phase"] = "day"
    state["votes"] = {}


def resolve_day(room):
    state = room["state"]
    votes = state["votes"]
    alive = state["alive"]

    if not votes:
        state["last_result"] = (
            "Голосування завершено. Ніхто не вибув."
        )

        eliminated = None

    else:
        counts = {}

        for target in votes.values():
            if target in alive:
                counts[target] = counts.get(target, 0) + 1

        if not counts:
            eliminated = None

        else:
            highest = max(counts.values())

            candidates = [
                name
                for name, count in counts.items()
                if count == highest
            ]

            eliminated = (
                candidates[0]
                if len(candidates) == 1
                else None
            )

        if eliminated:
            alive.remove(eliminated)

            state["last_result"] = (
                f"За результатами голосування "
                f"вибув: {eliminated}."
            )

        else:
            state["last_result"] = (
                "Нічия! Ніхто не вибув."
            )

    state["eliminated"] = eliminated
    state["votes"] = {}

    if mafia_finish(room):
        return

    state["round"] += 1
    state["phase"] = "night"
    state["night_actions"] = {}

    state["last_result"] += " Настала нова ніч."


def mafia_action(room, name, data):
    state = room["state"]
    roles = state["roles"]
    alive = state["alive"]

    if name not in alive:
        return

    if state["phase"] == "night":
        role = roles[name]
        action_type = data.get("type")
        target = data.get("target")

        if target not in alive or target == name:
            return

        if role == "mafia":
            if action_type != "kill":
                return

            if roles.get(target) == "mafia":
                return

        elif role == "doctor":
            if action_type != "save":
                return

        elif role == "detective":
            if action_type != "check":
                return

        else:
            return

        state["night_actions"][name] = {
            "type": action_type,
            "target": target
        }

        required = [
            player
            for player in alive
            if roles[player] in (
                "mafia",
                "doctor",
                "detective"
            )
        ]

        if all(
            player in state["night_actions"]
            for player in required
        ):
            resolve_night(room)

    elif state["phase"] == "day":
        target = data.get("target")

        if target not in alive or target == name:
            return

        state["votes"][name] = target

        if all(
            player in state["votes"]
            for player in alive
        ):
            resolve_day(room)


# =========================
# CROCODILE DENTIST
# =========================

def start_crocodile(room):
    if not 2 <= len(room["players"]) <= 4:
        raise HTTPException(
            400,
            "Для Крокодила потрібно від 2 до 4 гравців"
        )

    room["started"] = True

    room["state"] = {
        "turn": 0,
        "danger": random.randrange(12),
        "pressed": [],
        "alive": room["players"][:],
        "loser": None,
        "winner": None,
        "finished": False
    }


def crocodile_press(room, name, tooth):
    state = room["state"]
    alive = state["alive"]

    if (
        state["finished"]
        or not alive
        or alive[state["turn"] % len(alive)] != name
    ):
        return

    if (
        not isinstance(tooth, int)
        or not 0 <= tooth < 12
        or tooth in state["pressed"]
    ):
        return

    state["pressed"].append(tooth)

    if tooth == state["danger"]:
        state["loser"] = name
        alive.remove(name)

        if len(alive) == 1:
            state["winner"] = alive[0]
            state["finished"] = True
        else:
            state["turn"] %= len(alive)

    else:
        state["turn"] = (
            state["turn"] + 1
        ) % len(alive)
                # =========================
# DURAK (36-card, 2-6 players)
# =========================

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["6", "7", "8", "9", "10", "J", "Q", "K", "A"]


def make_deck():
    deck = [
        {
            "s": suit,
            "r": rank,
            "v": i + 6
        }
        for suit in SUITS
        for i, rank in enumerate(RANKS)
    ]

    random.shuffle(deck)
    return deck


def start_durak(room):
    n = len(room["players"])

    if not 2 <= n <= 6:
        raise HTTPException(
            400,
            "Для Дурня потрібно 2–6 гравців"
        )

    deck = make_deck()

    hands = {
        p: [deck.pop() for _ in range(6)]
        for p in room["players"]
    }

    trump = deck[-1]["s"] if deck else random.choice(SUITS)

    room["started"] = True

    room["state"] = {
        "hands": hands,
        "deck": deck,
        "trump": trump,
        "trump_card": deck[-1] if deck else None,
        "attacker": room["players"][0],
        "defender": room["players"][1],
        "table": [],
        "phase": "attack",
        "winner": None,
        "message": "Гра почалася. Атакуйте або відбивайтеся.",
        "players_order": room["players"][:],
        "loser": None
    }


def card_beats(defense, attack, trump):
    return (
        (
            defense["s"] == attack["s"]
            and defense["v"] > attack["v"]
        )
        or (
            defense["s"] == trump
            and attack["s"] != trump
        )
    )


def durak_refill(st):
    order = st["players_order"]
    attacker = st["attacker"]
    start = order.index(attacker)

    for k in range(len(order)):
        who = order[(start + k) % len(order)]

        while (
            len(st["hands"].get(who, [])) < 6
            and st["deck"]
        ):
            st["hands"][who].append(
                st["deck"].pop()
            )


def durak_next(st):
    order = st["players_order"]

    alive = [
        p
        for p in order
        if st["hands"].get(p)
    ]

    if len(alive) <= 1 and not st["deck"]:
        st["loser"] = alive[0] if alive else None

        st["winner"] = [
            p
            for p in order
            if p != st["loser"]
        ]

        st["phase"] = "finished"

        st["message"] = (
            "Дурень: " + str(st["loser"])
            if st["loser"]
            else "Нічия"
        )

        return

    current = st["attacker"]
    idx = order.index(current)

    candidates = [
        order[(idx + i) % len(order)]
        for i in range(1, len(order) + 1)
        if (
            order[(idx + i) % len(order)] in alive
            and order[(idx + i) % len(order)] != st["defender"]
        )
    ]

    if not candidates:
        st["phase"] = "finished"
        st["winner"] = current
        return

    st["attacker"] = st["defender"]
    st["defender"] = candidates[0]
    st["table"] = []
    st["phase"] = "attack"
    st["message"] = "Наступний раунд."


def durak_action(room, name, data):
    st = room["state"]

    if (
        st["phase"] == "finished"
        or name not in st["hands"]
    ):
        return

    hand = st["hands"][name]
    action = data.get("type")
    idx = data.get("card")

    if (
        action == "attack"
        and name == st["attacker"]
        and st["phase"] == "attack"
    ):
        if (
            not isinstance(idx, int)
            or not 0 <= idx < len(hand)
        ):
            return

        card = hand.pop(idx)

        if st["table"] and card["v"] not in (
            [x["attack"]["v"] for x in st["table"]]
            + [
                x["defense"]["v"]
                for x in st["table"]
                if x.get("defense")
            ]
        ):
            hand.insert(idx, card)
            return

        if len(st["table"]) >= 6:
            hand.insert(idx, card)
            return

        st["table"].append({
            "attack": card,
            "defense": None
        })

        st["phase"] = "defend"
        st["message"] = "Захисник має відбитися."

    elif (
        action == "defend"
        and name == st["defender"]
        and st["phase"] == "defend"
    ):
        pair = next(
            (
                x
                for x in st["table"]
                if x["defense"] is None
            ),
            None
        )

        if (
            not pair
            or not isinstance(idx, int)
            or not 0 <= idx < len(hand)
            or not card_beats(
                hand[idx],
                pair["attack"],
                st["trump"]
            )
        ):
            return

        pair["defense"] = hand.pop(idx)

        if all(x["defense"] for x in st["table"]):
            st["phase"] = "throw"
            st["message"] = (
                "Усі карти відбиті. "
                "Можна підкинути карту або завершити хід."
            )

    elif (
        action == "throw"
        and name != st["defender"]
        and st["phase"] in ("defend", "throw")
    ):
        if (
            not isinstance(idx, int)
            or not 0 <= idx < len(hand)
            or len(st["table"]) >= 6
        ):
            return

        card = hand[idx]

        ranks = (
            [x["attack"]["v"] for x in st["table"]]
            + [
                x["defense"]["v"]
                for x in st["table"]
                if x.get("defense")
            ]
        )

        if card["v"] not in ranks:
            return

        hand.pop(idx)

        st["table"].append({
            "attack": card,
            "defense": None
        })

        st["phase"] = "defend"

    elif (
        action in ("take", "pass")
        and name == st["defender"]
        and st["phase"] in ("defend", "throw")
    ):
        if action == "take":
            for pair in st["table"]:
                st["hands"][name].append(pair["attack"])

                if pair["defense"]:
                    st["hands"][name].append(pair["defense"])

            st["message"] = "Захисник забрав карти."

        else:
            if not all(
                x["defense"]
                for x in st["table"]
            ):
                return

            st["message"] = "Атака відбита."

        durak_refill(st)

        if action == "take":
            st["attacker"] = st["defender"]

        durak_next(st)


# =========================
# TEXAS HOLD'EM
# =========================

def poker_eval(cards):
    vals = sorted(
        [c["v"] for c in cards],
        reverse=True
    )

    counts = {
        v: vals.count(v)
        for v in set(vals)
    }

    groups = sorted(
        counts,
        key=lambda v: (counts[v], v),
        reverse=True
    )

    flush = any(
        sum(c["s"] == s for c in cards) >= 5
        for s in SUITS
    )

    unique = sorted(set(vals), reverse=True)
    straight = 0

    if 14 in unique:
        unique.append(1)

    for i in range(len(unique) - 4):
        if unique[i] - unique[i + 4] == 4:
            straight = unique[i]
            break

    if flush and straight:
        return (8, straight)

    if counts[groups[0]] == 4:
        return (7, groups[0], groups[1])

    if (
        counts[groups[0]] == 3
        and len(groups) > 1
        and counts[groups[1]] >= 2
    ):
        return (6, groups[0], groups[1])

    if flush:
        return (5, *sorted(vals, reverse=True)[:5])

    if straight:
        return (4, straight)

    if counts[groups[0]] == 3:
        return (
            3,
            groups[0],
            *sorted(
                [v for v in vals if v != groups[0]],
                reverse=True
            )[:2]
        )

    pairs = [
        v
        for v in groups
        if counts[v] == 2
    ]

    if len(pairs) >= 2:
        return (
            2,
            pairs[0],
            pairs[1],
            max(v for v in vals if v not in pairs[:2])
        )

    if pairs:
        return (
            1,
            pairs[0],
            *sorted(
                [v for v in vals if v != pairs[0]],
                reverse=True
            )[:3]
        )

    return (0, *vals[:5])


def start_poker(room):
    if not 2 <= len(room["players"]) <= 6:
        raise HTTPException(
            400,
            "Для покеру потрібно 2–6 гравців"
        )

    deck = make_deck()

    hands = {
        p: [deck.pop(), deck.pop()]
        for p in room["players"]
    }

    room["started"] = True

    room["state"] = {
        "deck": deck,
        "hands": hands,
        "community": [],
        "chips": {
            p: 1000
            for p in room["players"]
        },
        "bets": {
            p: 0
            for p in room["players"]
        },
        "pot": 0,
        "turn": 0,
        "players_order": room["players"][:],
        "folded": [],
        "phase": "preflop",
        "winner": None,
        "message": (
            "Texas Hold'em: ставка 10/20. "
            "Хід першого гравця."
        ),
        "acted": []
    }


def poker_action(room, name, data):
    st = room["state"]

    if (
        st["phase"] == "finished"
        or name in st["folded"]
    ):
        return

    alive = [
        p
        for p in st["players_order"]
        if p not in st["folded"]
    ]

    if len(alive) == 1:
        st["winner"] = alive[0]
        st["chips"][alive[0]] += st["pot"]
        st["phase"] = "finished"
        st["message"] = alive[0] + " забирає банк."
        return

    current = next(
        (
            p
            for p in alive
            if p not in st["acted"]
        ),
        alive[0]
    )

    if name != current:
        return

    action = data.get("type")
    highest = max(st["bets"].values(), default=0)
    owed = highest - st["bets"][name]

    if action == "fold":
        st["folded"].append(name)

    elif action in ("call", "check"):
        if action == "check" and owed > 0:
            return

        amount = min(owed, st["chips"][name])

        st["chips"][name] -= amount
        st["bets"][name] += amount
        st["pot"] += amount

    elif action == "raise":
        amount = max(20, int(data.get("amount", 20)))

        pay = min(
            st["chips"][name],
            owed + amount
        )

        st["chips"][name] -= pay
        st["bets"][name] += pay
        st["pot"] += pay
        st["acted"] = []

    else:
        return

    st["acted"].append(name)

    active = [
        p
        for p in alive
        if p not in st["folded"]
    ]

    if len(active) == 1:
        st["winner"] = active[0]
        st["chips"][active[0]] += st["pot"]
        st["phase"] = "finished"
        st["message"] = active[0] + " забирає банк."
        return

    if all(
        p in st["acted"]
        and st["bets"][p] == max(st["bets"].values())
        for p in active
    ):
        st["acted"] = []
        st["bets"] = {
            p: 0
            for p in st["players_order"]
        }

        if st["phase"] == "preflop":
            st["community"] += st["deck"][:3]
            del st["deck"][:3]
            st["phase"] = "flop"

        elif st["phase"] in ("flop", "turn"):
            st["community"].append(st["deck"].pop())

            st["phase"] = (
                "turn"
                if st["phase"] == "flop"
                else "river"
            )

        elif st["phase"] == "river":
            scores = {
                p: poker_eval(
                    st["hands"][p] + st["community"]
                )
                for p in active
            }

            best = max(scores.values())

            winners = [
                p
                for p, v in scores.items()
                if v == best
            ]

            share = st["pot"] // len(winners)

            for p in winners:
                st["chips"][p] += share

            st["winner"] = winners
            st["phase"] = "finished"
            st["message"] = (
                "Переможець: " + ", ".join(winners)
            )

    st["message"] = "Хід покеру оновлено."


# =========================
# PRIVATE ROOM VIEW
# =========================

def room_view(room, name):
    result = {
        "code": room["code"],
        "game": room["game"],
        "host": room["host"],
        "players": room["players"],
        "open": room["open"],
        "started": room["started"],
        "state": room["state"]
    }

    if not room["started"]:
        return result

    st = room["state"]

    if room["game"] == "mafia":
        result["state"] = {
            "phase": st["phase"],
            "round": st["round"],
            "alive": st["alive"],
            "winner": st["winner"],
            "last_result": st["last_result"],
            "eliminated": st["eliminated"],
            "votes_count": len(st["votes"]),
            "players_count": len(st["alive"])
        }

        result.update(
            my_role=st["roles"].get(name),
            my_alive=name in st["alive"],
            my_action_done=(
                name in st["night_actions"]
                if st["phase"] == "night"
                else name in st["votes"]
            ),
            my_check=st["private_checks"].get(name),
            mafia_teammates=(
                [
                    p
                    for p in st["alive"]
                    if st["roles"][p] == "mafia"
                ]
                if st["roles"].get(name) == "mafia"
                else []
            )
        )

    elif room["game"] == "durak":
        result["state"] = {
            k: v
            for k, v in st.items()
            if k not in ("hands", "deck")
        }

        result["state"]["deck_count"] = len(st["deck"])
        result["my_hand"] = st["hands"].get(name, [])

        result["hand_counts"] = {
            p: len(h)
            for p, h in st["hands"].items()
        }

    elif room["game"] == "poker":
        result["state"] = {
            k: v
            for k, v in st.items()
            if k not in ("hands", "deck")
        }

        result["my_hand"] = st["hands"].get(name, [])

        result["hand_counts"] = {
            p: (
                len(h)
                if p in st["folded"]
                else 2
            )
            for p, h in st["hands"].items()
        }

    return result


# =========================
# BROADCAST
# =========================

async def broadcast(code):
    room = rooms.get(code)

    if not room:
        return

    dead = []

    for ws, name in list(
        connections.get(code, {}).items()
    ):
        try:
            await ws.send_json(
                room_view(room, name)
            )

        except Exception:
            dead.append(ws)

    for ws in dead:
        connections.get(code, {}).pop(ws, None)


# =========================
# START GAME
# =========================

@app.post("/api/rooms/{code}/start")
async def start_room(code: str):
    room = rooms.get(code)

    if not room:
        raise HTTPException(404, "Кімнату не знайдено")

    if room["started"]:
        raise HTTPException(400, "Гра вже почалася")

    if room["game"] == "mafia":
        start_mafia(room)

    elif room["game"] == "ttt":
        if len(room["players"]) != 2:
            raise HTTPException(
                400,
                "Для хрестиків-нуликів потрібно рівно 2 гравці"
            )

        start_ttt(room)

    elif room["game"] == "crocodile":
        start_crocodile(room)

    elif room["game"] == "durak":
        start_durak(room)

    elif room["game"] == "poker":
        start_poker(room)

    else:
        raise HTTPException(400, "Невідома гра")

    await broadcast(code)

    return {
        "ok": True,
        "started": True
    }


# =========================
# WEBSOCKET
# =========================

@app.websocket("/ws/{code}")
async def websocket_room(
    ws: WebSocket,
    code: str,
    name: str = "Гравець"
):
    await ws.accept()

    room = rooms.get(code)

    if not room or name not in room["players"]:
        await ws.close(code=1008)
        return

    connections.setdefault(code, {})[ws] = name

    try:
        await ws.send_json(
            room_view(room, name)
        )

        while True:
            data = await ws.receive_json()
            room = rooms.get(code)

            if not room:
                break

            # =====================
            # TIC TAC TOE MOVE
            # =====================

            if (
                data.get("action") == "move"
                and room["game"] == "ttt"
                and room["started"]
            ):
                state = room["state"]
                index = data.get("index")

                if (
                    not isinstance(index, int)
                    or not 0 <= index < 9
                ):
                    continue

                board = state["board"]

                if board[index] or state["winner"]:
                    continue

                if name.casefold() not in [
                    p.casefold()
                    for p in room["players"]
                ]:
                    continue

                player_index = room["players"].index(name)

                symbol = (
                    "X"
                    if player_index == 0
                    else "O"
                )

                current = (
                    "X"
                    if state["turn"] % 2 == 0
                    else "O"
                )

                if symbol != current:
                    continue

                board[index] = symbol
                state["turn"] += 1

                wins = [
                    (0, 1, 2),
                    (3, 4, 5),
                    (6, 7, 8),
                    (0, 3, 6),
                    (1, 4, 7),
                    (2, 5, 8),
                    (0, 4, 8),
                    (2, 4, 6)
                ]

                for a, b, c in wins:
                    if (
                        board[a]
                        and board[a] == board[b] == board[c]
                    ):
                        state["winner"] = board[a]
                        break

                if (
                    not state["winner"]
                    and all(board)
                ):
                    state["winner"] = "draw"

                await broadcast(code)

            # =====================
            # CROCODILE DENTIST
            # =====================

            elif (
                data.get("action") == "crocodile_press"
                and room["game"] == "crocodile"
                and room["started"]
            ):
                crocodile_press(
                    room,
                    name,
                    data.get("tooth")
                )

                await broadcast(code)

            # =====================
            # MAFIA NIGHT ACTION
            # =====================

            elif (
                data.get("action") == "mafia"
                and room["game"] == "mafia"
                and room["started"]
            ):
                mafia_action(room, name, data)
                await broadcast(code)

            # =====================
            # MAFIA DAY VOTE
            # =====================

            elif (
                data.get("action") == "vote"
                and room["game"] == "mafia"
                and room["started"]
            ):
                mafia_action(
                    room,
                    name,
                    {
                        "type": "vote",
                        "target": data.get("target")
                    }
                )

                await broadcast(code)

            # =====================
            # DURAK ACTION
            # =====================

            elif (
                data.get("action") == "durak"
                and room["game"] == "durak"
                and room["started"]
            ):
                durak_action(room, name, data)
                await broadcast(code)

            # =====================
            # POKER ACTION
            # =====================

            elif (
                data.get("action") == "poker"
                and room["game"] == "poker"
                and room["started"]
            ):
                poker_action(room, name, data)
                await broadcast(code)

    except WebSocketDisconnect:
        pass

    except Exception as error:
        print("WebSocket error:", error)

    finally:
        connections.get(code, {}).pop(ws, None)
