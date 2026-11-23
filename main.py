
import os
import json
import uuid
import random
import sqlite3
import asyncio
import urllib.request
import urllib.error

from datetime import datetime, timezone
from typing import Optional, Any

from fastapi import (
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    Request,
    Header,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ============================================================
# ZENTRA API
# ============================================================

app = FastAPI(title="ZENTRA API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# CONFIGURATION
# ============================================================

DB_PATH = os.getenv("ZENTRA_DB", "zentra.db")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

PREMIUM_PRICE = 20

MAX_NICKNAME_LENGTH = 20
MIN_NICKNAME_LENGTH = 3

SUPPORTED_GAMES = {
    "ttt",
    "crocodile",
    "durak",
    "poker",
    "mafia",
}

rooms = {}
connections = {}


# ============================================================
# DATABASE
# ============================================================

def db():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    connection = db()

    connection.execute("""
        CREATE TABLE IF NOT EXISTS users (
            uid TEXT PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            premium INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL,
            telegram_charge_id TEXT NOT NULL UNIQUE,
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    connection.commit()
    connection.close()


init_db()


# ============================================================
# MODELS
# ============================================================

class Profile(BaseModel):
    uid: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=MIN_NICKNAME_LENGTH,
                      max_length=MAX_NICKNAME_LENGTH)


class RoomCreate(BaseModel):
    name: str = Field(min_length=MIN_NICKNAME_LENGTH,
                      max_length=MAX_NICKNAME_LENGTH)
    game: str = "ttt"
    open: bool = False


class JoinRoom(BaseModel):
    code: str = Field(min_length=4, max_length=12)
    name: str = Field(min_length=MIN_NICKNAME_LENGTH,
                      max_length=MAX_NICKNAME_LENGTH)


# ============================================================
# COMMON HELPERS
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_name(name: str) -> str:
    if not isinstance(name, str):
        raise HTTPException(400, "Invalid nickname")

    name = " ".join(name.strip().split())

    if not MIN_NICKNAME_LENGTH <= len(name) <= MAX_NICKNAME_LENGTH:
        raise HTTPException(
            400,
            f"Nickname must contain {MIN_NICKNAME_LENGTH}-{MAX_NICKNAME_LENGTH} characters"
        )

    if any(ord(char) < 32 for char in name):
        raise HTTPException(400, "Invalid nickname")

    return name


def get_user(uid: str):
    connection = db()

    user = connection.execute(
        "SELECT uid, name, premium FROM users WHERE uid = ?",
        (uid,)
    ).fetchone()

    connection.close()

    return dict(user) if user else None


def is_premium(uid: str) -> bool:
    user = get_user(uid)
    return bool(user and user["premium"])


def new_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    while True:
        code = "".join(random.choices(alphabet, k=6))

        if code not in rooms:
            return code


def get_room(code: str):
    room = rooms.get(code.upper())

    if not room:
        raise HTTPException(404, "Room not found")

    return room


def public_room(room):
    state = room.get("state", {})

    result = {
        "code": room["code"],
        "game": room["game"],
        "host": room["host"],
        "players": list(room["players"]),
        "open": room["open"],
        "started": room["started"],
        "max_players": room["max_players"],
    }

    if room["game"] == "ttt":
        result["state"] = state

    elif room["game"] == "crocodile":
        result["state"] = state

    elif room["game"] == "mafia":
        result["state"] = {
            "phase": state.get("phase"),
            "round": state.get("round"),
            "alive": state.get("alive", []),
            "winner": state.get("winner"),
            "last_result": state.get("last_result"),
            "eliminated": state.get("eliminated"),
        }

    elif room["game"] == "durak":
        result["state"] = {
            "phase": state.get("phase"),
            "trump": state.get("trump"),
            "trump_card": state.get("trump_card"),
            "attacker": state.get("attacker"),
            "defender": state.get("defender"),
            "table": state.get("table", []),
            "winner": state.get("winner"),
            "message": state.get("message"),
            "hand_counts": {
                name: len(cards)
                for name, cards in state.get("hands", {}).items()
            },
            "deck_count": len(state.get("deck", [])),
        }

    elif room["game"] == "poker":
        result["state"] = {
            "community": state.get("community", []),
            "chips": state.get("chips", {}),
            "bets": state.get("bets", {}),
            "pot": state.get("pot", 0),
            "turn": state.get("turn"),
            "phase": state.get("phase"),
            "winner": state.get("winner"),
            "message": state.get("message"),
            "hand_counts": {
                name: len(cards)
                for name, cards in state.get("hands", {}).items()
            },
        }

    return result


# ============================================================
# TELEGRAM API
# ============================================================

def telegram_api(method: str, payload: dict):
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(
            503,
            "Telegram bot token is not configured"
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/{method}"
    )

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise HTTPException(502, f"Telegram API error: {details}")

    except Exception as error:
        raise HTTPException(502, f"Telegram connection error: {error}")

    if not result.get("ok"):
        raise HTTPException(502, "Telegram API request failed")

    return result.get("result")


# ============================================================
# ROOT / HEALTH
# ============================================================

@app.get("/")
async def root():
    return {
        "name": "ZENTRA API",
        "status": "online",
        "version": "2.0",
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "rooms": len(rooms),
        "connections": sum(
            len(items) for items in connections.values()
        ),
    }


# ============================================================
# PROFILES
# ============================================================

@app.post("/api/profile")
async def create_profile(profile: Profile):
    uid = profile.uid.strip()
    name = clean_name(profile.name)

    if not uid:
        raise HTTPException(400, "Invalid user ID")

    connection = db()

    existing_uid = connection.execute(
        "SELECT uid, name, premium FROM users WHERE uid = ?",
        (uid,)
    ).fetchone()

    existing_name = connection.execute(
        "SELECT uid FROM users WHERE name = ? COLLATE NOCASE",
        (name,)
    ).fetchone()

    if existing_uid:
        connection.close()
        raise HTTPException(
            409,
            "Profile already exists"
        )

    if existing_name:
        connection.close()
        raise HTTPException(
            409,
            "This nickname is already taken"
        )

    try:
        connection.execute(
            """
            INSERT INTO users (uid, name, premium, created_at)
            VALUES (?, ?, 0, ?)
            """,
            (uid, name, now_iso())
        )

        connection.commit()

    except sqlite3.IntegrityError:
        connection.close()
        raise HTTPException(
            409,
            "This nickname is already taken"
        )

    connection.close()

    return {
        "success": True,
        "uid": uid,
        "name": name,
        "premium": False,
    }


@app.get("/api/profile/{uid}")
async def profile(uid: str):
    user = get_user(uid)

    if not user:
        raise HTTPException(404, "Profile not found")

    return {
        "uid": user["uid"],
        "name": user["name"],
        "premium": bool(user["premium"]),
    }


# ============================================================
# PREMIUM — TELEGRAM STARS
# ============================================================

@app.post("/api/premium/invoice")
async def create_premium_invoice(data: dict):
    uid = str(data.get("uid", "")).strip()

    if not uid:
        raise HTTPException(400, "User ID is required")

    user = get_user(uid)

    if not user:
        raise HTTPException(404, "Profile not found")

    if user["premium"]:
        return {
            "success": True,
            "already_premium": True,
        }

    payload = f"zentra_premium:{uid}"

    invoice_link = telegram_api(
        "createInvoiceLink",
        {
            "title": "ZENTRA PREMIUM",
            "description": (
                "Lifetime ZENTRA PREMIUM access. "
                "Unlock premium games and future premium features."
            ),
            "payload": payload,
            "provider_token": "",
            "currency": "XTR",
            "prices": [
                {
                    "label": "ZENTRA PREMIUM lifetime",
                    "amount": PREMIUM_PRICE,
                }
            ],
        }
    )

    return {
        "success": True,
        "invoice_url": invoice_link,
        "price": PREMIUM_PRICE,
        "currency": "XTR",
    }


# ============================================================
# TELEGRAM PAYMENT WEBHOOK
# ============================================================

@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(
        default=None
    ),
):
    if TELEGRAM_WEBHOOK_SECRET:
        if x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
            raise HTTPException(403, "Invalid webhook secret")

    update = await request.json()

    # --------------------------------------------------------
    # PRE-CHECKOUT
    # --------------------------------------------------------

    checkout = update.get("pre_checkout_query")

    if checkout:
        payload = checkout.get("invoice_payload", "")
        currency = checkout.get("currency")
        amount = checkout.get("total_amount")

        valid = (
            payload.startswith("zentra_premium:")
            and currency == "XTR"
            and amount == PREMIUM_PRICE
        )

        uid = payload.replace("zentra_premium:", "", 1)

        if valid and get_user(uid):
            telegram_api(
                "answerPreCheckoutQuery",
                {
                    "pre_checkout_query_id": checkout["id"],
                    "ok": True,
                }
            )
        else:
            telegram_api(
                "answerPreCheckoutQuery",
                {
                    "pre_checkout_query_id": checkout["id"],
                    "ok": False,
                    "error_message": "Invalid ZENTRA PREMIUM payment",
                }
            )

        return {"ok": True}

    # --------------------------------------------------------
    # SUCCESSFUL PAYMENT
    # --------------------------------------------------------

    message = update.get("message", {})
    payment = message.get("successful_payment")

    if payment:
        payload = payment.get("invoice_payload", "")
        currency = payment.get("currency")
        amount = payment.get("total_amount")
        charge_id = payment.get("telegram_payment_charge_id")

        prefix = "zentra_premium:"

        if not payload.startswith(prefix):
            return {"ok": True}

        uid = payload[len(prefix):]

        if (
            currency != "XTR"
            or amount != PREMIUM_PRICE
            or not charge_id
        ):
            return {"ok": True}

        connection = db()

        user = connection.execute(
            "SELECT uid FROM users WHERE uid = ?",
            (uid,)
        ).fetchone()

        if user:
            try:
                connection.execute(
                    """
                    INSERT INTO payments (
                        uid,
                        telegram_charge_id,
                        amount,
                        currency,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        charge_id,
                        amount,
                        currency,
                        now_iso(),
                    )
                )

                connection.execute(
                    "UPDATE users SET premium = 1 WHERE uid = ?",
                    (uid,)
                )

                connection.commit()

            except sqlite3.IntegrityError:
                # Payment already processed.
                pass

        connection.close()

    return {"ok": True}


# ============================================================
# ROOMS
# ============================================================

def room_max_players(game: str) -> int:
    limits = {
        "ttt": 2,
        "crocodile": 4,
        "durak": 6,
        "poker": 6,
        "mafia": 10,
    }

    return limits[game]


@app.post("/api/rooms")
async def create_room(data: RoomCreate):
    name = clean_name(data.name)
    game = data.game.lower().strip()

    if game not in SUPPORTED_GAMES:
        raise HTTPException(400, "Unknown game")

    code = new_code()

    room = {
        "code": code,
        "game": game,
        "host": name,
        "players": [name],
        "open": bool(data.open),
        "started": False,
        "max_players": room_max_players(game),
        "state": {},
        "created_at": now_iso(),
    }

    rooms[code] = room

    return {
        "success": True,
        "room": public_room(room),
    }


@app.get("/api/rooms")
async def list_rooms():
    return [
        public_room(room)
        for room in rooms.values()
    ]


@app.get("/api/lobbies")
async def list_lobbies():
    return [
        public_room(room)
        for room in rooms.values()
        if room["open"] and not room["started"]
    ]


@app.post("/api/rooms/join")
async def join_room(data: JoinRoom):
    code = data.code.upper().strip()
    name = clean_name(data.name)

    room = rooms.get(code)

    if not room:
        raise HTTPException(404, "Room not found")

    if room["started"]:
        raise HTTPException(400, "Game already started")

    if name in room["players"]:
        return {
            "success": True,
            "room": public_room(room),
        }

    if len(room["players"]) >= room["max_players"]:
        raise HTTPException(400, "Room is full")

    if any(
        player.casefold() == name.casefold()
        for player in room["players"]
    ):
        raise HTTPException(409, "Nickname already in room")

    room["players"].append(name)

    return {
        "success": True,
        "room": public_room(room),
    }


@app.get("/api/rooms/{code}")
async def get_room_info(code: str):
    room = get_room(code)
    return public_room(room)

# ============================================================
# TIC TAC TOE
# ============================================================

def start_ttt(room):
    if len(room["players"]) != 2:
        raise HTTPException(400, "Tic-tac-toe requires exactly 2 players")

    room["started"] = True
    room["state"] = {
        "board": [""] * 9,
        "symbols": {
            room["players"][0]: "X",
            room["players"][1]: "O",
        },
        "turn": room["players"][0],
        "winner": None,
        "draw": False,
        "finished": False,
    }


def ttt_move(room, name, index):
    state = room["state"]

    if not room["started"] or state.get("finished"):
        raise HTTPException(400, "Game is not active")

    if name != state["turn"]:
        raise HTTPException(400, "Not your turn")

    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 9:
        raise HTTPException(400, "Invalid cell")

    if state["board"][index]:
        raise HTTPException(400, "Cell already occupied")

    symbol = state["symbols"][name]
    state["board"][index] = symbol

    wins = [
        (0, 1, 2), (3, 4, 5), (6, 7, 8),
        (0, 3, 6), (1, 4, 7), (2, 5, 8),
        (0, 4, 8), (2, 4, 6),
    ]

    for a, b, c in wins:
        if (
            state["board"][a]
            == state["board"][b]
            == state["board"][c]
            == symbol
        ):
            state["winner"] = name
            state["finished"] = True
            return

    if all(state["board"]):
        state["draw"] = True
        state["finished"] = True
        return

    other = next(
        player for player in room["players"]
        if player != name
    )

    state["turn"] = other


# ============================================================
# CROCODILE DENTIST
# ============================================================

def start_crocodile(room):
    count = len(room["players"])

    if not 2 <= count <= 4:
        raise HTTPException(
            400,
            "Crocodile Dentist requires 2-4 players"
        )

    teeth = list(range(12))
    danger = random.choice(teeth)

    room["started"] = True
    room["state"] = {
        "turn": room["players"][0],
        "danger": danger,
        "pressed": [],
        "alive": list(room["players"]),
        "loser": None,
        "winner": None,
        "finished": False,
        "message": "Game started",
    }


def crocodile_press(room, name, tooth):
    state = room["state"]

    if not room["started"] or state["finished"]:
        raise HTTPException(400, "Game is not active")

    if name != state["turn"]:
        raise HTTPException(400, "Not your turn")

    if not isinstance(tooth, int) or isinstance(tooth, bool) or not 0 <= tooth < 12:
        raise HTTPException(400, "Invalid tooth")

    if tooth in state["pressed"]:
        raise HTTPException(400, "This tooth was already pressed")

    state["pressed"].append(tooth)

    if tooth == state["danger"]:
        state["loser"] = name
        state["alive"].remove(name)
        state["message"] = f"{name} lost!"

        if len(state["alive"]) == 1:
            state["winner"] = state["alive"][0]
            state["finished"] = True
            return

        # A new round begins with a new dangerous tooth.
        state["danger"] = random.choice(
            [i for i in range(12) if i not in state["pressed"]]
            or list(range(12))
        )
        state["pressed"] = []

        state["turn"] = state["alive"][
            0
        ]

        return

    alive = state["alive"]
    current_index = alive.index(name)
    state["turn"] = alive[(current_index + 1) % len(alive)]
    state["message"] = f"{name} pressed tooth {tooth + 1}"


# ============================================================
# MAFIA
# ============================================================

def mafia_roles(count):
    roles = ["mafia"] * max(1, count // 4)

    if count >= 5:
        roles.append("doctor")

    if count >= 5:
        roles.append("detective")

    while len(roles) < count:
        roles.append("citizen")

    random.shuffle(roles)
    return roles


def start_mafia(room):
    count = len(room["players"])

    if not 4 <= count <= 10:
        raise HTTPException(
            400,
            "Mafia requires 4-10 players"
        )

    assigned = mafia_roles(count)

    roles = {
        player: assigned[index]
        for index, player in enumerate(room["players"])
    }

    room["started"] = True
    room["state"] = {
        "phase": "night",
        "round": 1,
        "alive": list(room["players"]),
        "roles": roles,
        "night_actions": {},
        "votes": {},
        "winner": None,
        "last_result": "Night has begun",
        "private_checks": {},
        "eliminated": None,
        "action_done": [],
    }


def mafia_finish(room):
    state = room["state"]

    alive = state["alive"]
    roles = state["roles"]

    mafia_count = sum(
        1 for player in alive
        if roles[player] == "mafia"
    )

    other_count = len(alive) - mafia_count

    if mafia_count == 0:
        state["winner"] = "citizens"
        state["phase"] = "finished"
        state["last_result"] = "Citizens win!"

    elif mafia_count >= other_count:
        state["winner"] = "mafia"
        state["phase"] = "finished"
        state["last_result"] = "Mafia wins!"


def resolve_night(room):
    state = room["state"]

    alive = state["alive"]
    roles = state["roles"]
    actions = state["night_actions"]

    mafia_votes = [
        target
        for player, target in actions.items()
        if (
            player in alive
            and roles[player] == "mafia"
            and target in alive
            and roles[target] != "mafia"
        )
    ]

    doctor_target = next(
        (
            target
            for player, target in actions.items()
            if player in alive
            and roles[player] == "doctor"
            and target in alive
        ),
        None,
    )

    detective_action = next(
        (
            (player, target)
            for player, target in actions.items()
            if player in alive
            and roles[player] == "detective"
            and target in alive
        ),
        None,
    )

    if detective_action:
        detective, target = detective_action
        state["private_checks"][detective] = (
            roles[target] == "mafia"
        )

    eliminated = None

    if mafia_votes:
        counts = {}

        for target in mafia_votes:
            counts[target] = counts.get(target, 0) + 1

        maximum = max(counts.values())
        candidates = [
            target for target, amount in counts.items()
            if amount == maximum
        ]

        if len(candidates) == 1:
            victim = candidates[0]

            if victim != doctor_target:
                eliminated = victim

    if eliminated:
        state["alive"].remove(eliminated)
        state["eliminated"] = eliminated
        state["last_result"] = f"{eliminated} was eliminated"
    else:
        state["eliminated"] = None
        state["last_result"] = "Nobody was eliminated"

    state["night_actions"] = {}
    state["votes"] = {}
    state["action_done"] = []

    mafia_finish(room)

    if state["phase"] != "finished":
        state["phase"] = "day"


def resolve_day(room):
    state = room["state"]

    votes = state["votes"]
    alive = state["alive"]

    counts = {}

    for voter, target in votes.items():
        if voter in alive and target in alive:
            counts[target] = counts.get(target, 0) + 1

    eliminated = None

    if counts:
        maximum = max(counts.values())

        candidates = [
            target for target, amount in counts.items()
            if amount == maximum
        ]

        if len(candidates) == 1:
            eliminated = candidates[0]

    if eliminated:
        state["alive"].remove(eliminated)
        state["eliminated"] = eliminated
        state["last_result"] = f"{eliminated} was voted out"
    else:
        state["eliminated"] = None
        state["last_result"] = "No one was eliminated"

    state["votes"] = {}

    mafia_finish(room)

    if state["phase"] != "finished":
        state["round"] += 1
        state["phase"] = "night"
        state["night_actions"] = {}
        state["action_done"] = []


def mafia_action(room, name, action):
    state = room["state"]

    if not room["started"] or state["phase"] == "finished":
        raise HTTPException(400, "Game is finished")

    if name not in state["alive"]:
        raise HTTPException(400, "You are eliminated")

    action_type = action.get("type")
    target = action.get("target")

    if target not in state["alive"]:
        raise HTTPException(400, "Invalid target")

    if target == name:
        raise HTTPException(400, "You cannot target yourself")

    role = state["roles"][name]

    if state["phase"] == "night":
        if name in state["action_done"]:
            raise HTTPException(400, "You already acted this night")

        if action_type != "night":
            raise HTTPException(400, "Invalid night action")

        if role == "mafia":
            if state["roles"][target] == "mafia":
                raise HTTPException(400, "You cannot target another mafia member")

        elif role == "doctor":
            pass

        elif role == "detective":
            pass

        else:
            raise HTTPException(400, "Citizens cannot act at night")

        state["night_actions"][name] = target
        state["action_done"].append(name)

        active_roles = [
            player for player in state["alive"]
            if state["roles"][player] in ("mafia", "doctor", "detective")
        ]

        if all(
            player in state["action_done"]
            for player in active_roles
        ):
            resolve_night(room)

    elif state["phase"] == "day":
        if action_type != "vote":
            raise HTTPException(400, "Invalid day action")

        if name in state["votes"]:
            raise HTTPException(400, "You already voted")

        state["votes"][name] = target

        if all(
            player in state["votes"]
            for player in state["alive"]
        ):
            resolve_day(room)

    else:
        raise HTTPException(400, "Invalid game phase")


# ============================================================
# DURAK
# ============================================================

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = [
    "6", "7", "8", "9", "10",
    "J", "Q", "K", "A",
]


def make_deck():
    deck = [
        {
            "rank": rank,
            "suit": suit,
            "value": index + 6,
            "id": f"{rank}{suit}",
        }
        for suit in SUITS
        for index, rank in enumerate(RANKS)
    ]

    random.shuffle(deck)
    return deck


def start_durak(room):
    count = len(room["players"])

    if not 2 <= count <= 6:
        raise HTTPException(
            400,
            "Durak requires 2-6 players"
        )

    deck = make_deck()
    hands = {
        player: []
        for player in room["players"]
    }

    for _ in range(6):
        for player in room["players"]:
            if deck:
                hands[player].append(deck.pop())

    trump_card = deck[-1] if deck else None

    if trump_card:
        trump = trump_card["suit"]
    else:
        trump = random.choice(SUITS)

    room["started"] = True
    room["state"] = {
        "hands": hands,
        "deck": deck,
        "trump": trump,
        "trump_card": trump_card,
        "attacker": room["players"][0],
        "defender": room["players"][1],
        "table": [],
        "phase": "attack",
        "winner": None,
        "message": "Durak started",
        "players_order": list(room["players"]),
        "loser": None,
        "passed": [],
        "max_attack": 6,
    }


def card_beats(attack, defense, trump):
    if defense["suit"] == attack["suit"]:
        return defense["value"] > attack["value"]

    return (
        defense["suit"] == trump
        and attack["suit"] != trump
    )


def durak_refill(state):
    order = state["players_order"]

    if state["attacker"] in order:
        start = order.index(state["attacker"])
    else:
        start = 0

    refill_order = order[start:] + order[:start]

    for player in refill_order:
        while (
            len(state["hands"][player]) < 6
            and state["deck"]
        ):
            state["hands"][player].append(
                state["deck"].pop()
            )


def durak_next(room, defender_took=False):
    state = room["state"]

    order = state["players_order"]
    old_attacker = state["attacker"]
    old_defender = state["defender"]

    if defender_took:
        state["hands"][old_defender].extend(
            card
            for pair in state["table"]
            for card in (
                pair["attack"],
                pair.get("defense")
            )
            if card is not None
        )

        next_index = order.index(old_defender)
        next_attacker = old_defender

    else:
        next_index = order.index(old_attacker)
        next_attacker = old_attacker

    durak_refill(state)

    remaining = [
        player for player in order
        if state["hands"][player] or state["deck"]
    ]

    if len(remaining) <= 1 and not state["deck"]:
        state["winner"] = remaining[0] if remaining else None
        state["loser"] = old_defender if defender_took else None
        state["phase"] = "finished"
        state["message"] = "Game finished"
        return

    if next_attacker not in remaining:
        next_attacker = remaining[0]

    attacker_index = order.index(next_attacker)

    possible_defenders = [
        order[(attacker_index + offset) % len(order)]
        for offset in range(1, len(order))
        if order[(attacker_index + offset) % len(order)] in remaining
    ]

    if not possible_defenders:
        state["winner"] = next_attacker
        state["phase"] = "finished"
        return

    state["attacker"] = next_attacker
    state["defender"] = possible_defenders[0]
    state["table"] = []
    state["passed"] = []
    state["phase"] = "attack"
    state["message"] = "New turn"


def durak_action(room, name, data):
    state = room["state"]

    if not room["started"] or state["phase"] == "finished":
        raise HTTPException(400, "Game is finished")

    if name not in state["hands"]:
        raise HTTPException(400, "Unknown player")

    action_type = data.get("type")
    card_id = data.get("card")

    hand = state["hands"][name]

    def find_card():
        for card in hand:
            if card["id"] == card_id:
                return card
        raise HTTPException(400, "Card not found in your hand")

    if action_type == "attack":
        if name != state["attacker"]:
            raise HTTPException(400, "Only attacker can attack")

        if state["phase"] != "attack":
            raise HTTPException(400, "Cannot attack now")

        card = find_card()

        if len(state["table"]) >= state["max_attack"]:
            raise HTTPException(400, "Attack limit reached")

        if state["table"]:
            table_values = [
                pair["attack"]["value"]
                for pair in state["table"]
            ] + [
                pair["defense"]["value"]
                for pair in state["table"]
                if pair.get("defense")
            ]

            if card["value"] not in table_values:
                raise HTTPException(
                    400,
                    "Card rank must match a card on the table"
                )

        hand.remove(card)

        state["table"].append({
            "attack": card,
            "defense": None,
        })

        state["phase"] = "defend"
        state["message"] = f"{name} attacks"

    elif action_type == "throw":
        if name == state["defender"]:
            raise HTTPException(400, "Defender cannot throw in")

        if state["phase"] != "defend":
            raise HTTPException(400, "Cannot throw in now")

        if name in state["passed"]:
            raise HTTPException(400, "You already passed")

        if len(state["table"]) >= state["max_attack"]:
            raise HTTPException(400, "Attack limit reached")

        card = find_card()

        table_values = [
            pair["attack"]["value"]
            for pair in state["table"]
        ] + [
            pair["defense"]["value"]
            for pair in state["table"]
            if pair.get("defense")
        ]

        if card["value"] not in table_values:
            raise HTTPException(400, "Card rank does not match the table")

        hand.remove(card)

        state["table"].append({
            "attack": card,
            "defense": None,
        })

        state["message"] = f"{name} throws in"

    elif action_type == "defend":
        if name != state["defender"]:
            raise HTTPException(400, "Only defender can defend")

        if state["phase"] != "defend":
            raise HTTPException(400, "Cannot defend now")

        card = find_card()

        target_index = data.get("target", len(state["table"]) - 1)

        if (
            not isinstance(target_index, int)
            or isinstance(target_index, bool)
            or not 0 <= target_index < len(state["table"])
        ):
            raise HTTPException(400, "Invalid attack index")

        pair = state["table"][target_index]

        if pair["defense"] is not None:
            raise HTTPException(400, "Attack already defended")

        if not card_beats(pair["attack"], card, state["trump"]):
            raise HTTPException(400, "This card cannot beat the attack")

        hand.remove(card)
        pair["defense"] = card

        if all(pair["defense"] for pair in state["table"]):
            state["phase"] = "take_or_end"

        state["message"] = f"{name} defended"

    elif action_type == "take":
        if name != state["defender"]:
            raise HTTPException(400, "Only defender can take")

        if state["phase"] not in ("defend", "take_or_end"):
            raise HTTPException(400, "Cannot take now")

        durak_next(room, defender_took=True)

    elif action_type == "pass":
        if name == state["defender"]:
            raise HTTPException(400, "Defender cannot pass")

        if state["phase"] != "defend":
            raise HTTPException(400, "Cannot pass now")

        if name in state["passed"]:
            raise HTTPException(400, "Already passed")

        state["passed"].append(name)

        eligible = [
            player for player in state["players_order"]
            if player not in (state["attacker"], state["defender"])
            and state["hands"][player]
        ]

        if all(
            player in state["passed"]
            for player in eligible
        ):
            state["message"] = "All players passed"

    elif action_type == "end":
        if name != state["defender"]:
            raise HTTPException(400, "Only defender can finish the turn")

        if state["phase"] != "take_or_end":
            raise HTTPException(400, "You must defend all attacks first")

        durak_next(room, defender_took=False)

    else:
        raise HTTPException(400, "Unknown Durak action")


# ============================================================
# POKER — TEXAS HOLD'EM
# ============================================================

POKER_SUITS = ["♠", "♥", "♦", "♣"]
POKER_RANKS = list(range(2, 15))


def poker_card_name(card):
    rank_names = {
        11: "J",
        12: "Q",
        13: "K",
        14: "A",
    }

    rank = rank_names.get(card["rank"], str(card["rank"]))
    return f"{rank}{card['suit']}"


def poker_deck():
    deck = [
        {
            "rank": rank,
            "suit": suit,
            "id": f"{rank}{suit}",
        }
        for suit in POKER_SUITS
        for rank in POKER_RANKS
    ]

    random.shuffle(deck)
    return deck


def poker_straight_high(values):
    unique = sorted(set(values), reverse=True)

    if 14 in unique:
        unique.append(1)

    for index in range(len(unique) - 4):
        sequence = unique[index:index + 5]

        if (
            len(sequence) == 5
            and sequence[0] - sequence[4] == 4
        ):
            return sequence[0]

    return None


def poker_eval(cards):
    values = [card["rank"] for card in cards]

    counts = {}

    for value in values:
        counts[value] = counts.get(value, 0) + 1

    groups = sorted(
        counts.items(),
        key=lambda item: (item[1], item[0]),
        reverse=True,
    )

    flush_suit = None

    for suit in POKER_SUITS:
        suited = [
            card["rank"]
            for card in cards
            if card["suit"] == suit
        ]

        if len(suited) >= 5:
            flush_suit = suit
            break

    straight_high = poker_straight_high(values)

    if flush_suit:
        flush_values = [
            card["rank"]
            for card in cards
            if card["suit"] == flush_suit
        ]

        flush_straight = poker_straight_high(flush_values)

        if flush_straight:
            return (8, flush_straight)

    four = next(
        (value for value, count in groups if count == 4),
        None,
    )

    if four:
        kicker = max(
            value for value in values
            if value != four
        )
        return (7, four, kicker)

    triples = sorted(
        [value for value, count in groups if count >= 3],
        reverse=True,
    )

    pairs = sorted(
        [value for value, count in groups if count >= 2],
        reverse=True,
    )

    if triples:
        full_pair = next(
            (
                value for value in pairs
                if value != triples[0]
            ),
            None,
        )

        if full_pair:
            return (6, triples[0], full_pair)

    if flush_suit:
        suited = sorted(
            [
                card["rank"]
                for card in cards
                if card["suit"] == flush_suit
            ],
            reverse=True,
        )

        return (5, *suited[:5])

    if straight_high:
        return (4, straight_high)

    if triples:
        kickers = sorted(
            [
                value for value in values
                if value != triples[0]
            ],
            reverse=True,
        )

        return (3, triples[0], *kickers[:2])

    if len(pairs) >= 2:
        high_pair, low_pair = pairs[:2]

        kicker = max(
            value for value in values
            if value not in (high_pair, low_pair)
        )

        return (2, high_pair, low_pair, kicker)

    if pairs:
        pair = pairs[0]

        kickers = sorted(
            [
                value for value in values
                if value != pair
            ],
            reverse=True,
        )

        return (1, pair, *kickers[:3])

    return (0, *sorted(values, reverse=True)[:5])


def poker_best_hand(cards):
    from itertools import combinations

    if len(cards) < 5:
        return poker_eval(cards)

    return max(
        poker_eval(list(combo))
        for combo in combinations(cards, 5)
    )


def start_poker(room):
    count = len(room["players"])

    if not 2 <= count <= 6:
        raise HTTPException(
            400,
            "Poker requires 2-6 players"
        )

    deck = poker_deck()

    hands = {
        player: [deck.pop(), deck.pop()]
        for player in room["players"]
    }

    chips = {
        player: 1000
        for player in room["players"]
    }

    active = list(room["players"])

    room["started"] = True
    room["state"] = {
        "deck": deck,
        "hands": hands,
        "community": [],
        "chips": chips,
        "bets": {player: 0 for player in active},
        "pot": 0,
        "turn": active[0],
        "players_order": active,
        "folded": [],
        "phase": "preflop",
        "winner": None,
        "winners": [],
        "message": "Texas Hold'em started",
        "acted": [],
        "current_bet": 0,
        "round_start": active[0],
        "finished": False,
    }


def poker_next_active(state, current):
    order = state["players_order"]
    active = [
        player for player in order
        if player not in state["folded"]
        and state["chips"][player] > 0
    ]

    if not active:
        return None

    if current not in order:
        return active[0]

    index = order.index(current)

    for offset in range(1, len(order) + 1):
        candidate = order[(index + offset) % len(order)]

        if candidate in active:
            return candidate

    return None


def poker_finish_hand(state):
    active = [
        player for player in state["players_order"]
        if player not in state["folded"]
    ]

    if len(active) == 1:
        winner = active[0]
        state["chips"][winner] += state["pot"]
        state["winners"] = [winner]
        state["winner"] = winner
        state["pot"] = 0
        state["phase"] = "finished"
        state["finished"] = True
        state["message"] = f"{winner} wins the pot"
        return True

    return False


def poker_showdown(state):
    active = [
        player for player in state["players_order"]
        if player not in state["folded"]
    ]

    while len(state["community"]) < 5 and state["deck"]:
        state["community"].append(state["deck"].pop())

    scores = {
        player: poker_best_hand(
            state["hands"][player] + state["community"]
        )
        for player in active
    }

    best_score = max(scores.values())

    winners = [
        player for player, score in scores.items()
        if score == best_score
    ]

    if winners:
        share = state["pot"] // len(winners)
        remainder = state["pot"] % len(winners)

        for index, player in enumerate(winners):
            state["chips"][player] += share + (
                1 if index < remainder else 0
            )

    state["winners"] = winners
    state["winner"] = winners[0] if len(winners) == 1 else None
    state["pot"] = 0
    state["phase"] = "finished"
    state["finished"] = True
    state["message"] = (
        f"{', '.join(winners)} won the hand"
    )


def poker_advance_phase(state):
    phases = [
        "preflop",
        "flop",
        "turn",
        "river",
    ]

    if state["phase"] not in phases:
        return

    current_index = phases.index(state["phase"])

    if current_index == 0:
        state["community"].extend(
            state["deck"].pop()
            for _ in range(3)
        )

    elif current_index < 3:
        state["community"].append(state["deck"].pop())

    else:
        poker_showdown(state)
        return

    state["phase"] = phases[current_index + 1]
    state["bets"] = {
        player: 0
        for player in state["players_order"]
    }
    state["current_bet"] = 0
    state["acted"] = []

    active = [
        player for player in state["players_order"]
        if player not in state["folded"]
        and state["chips"][player] > 0
    ]

    if active:
        state["turn"] = active[0]


def poker_action(room, name, data):
    state = room["state"]

    if (
        not room["started"]
        or state.get("finished")
        or state["phase"] == "finished"
    ):
        raise HTTPException(400, "Poker hand is finished")

    if name != state["turn"]:
        raise HTTPException(400, "Not your turn")

    if name in state["folded"]:
        raise HTTPException(400, "You have folded")

    action_type = data.get("type")

    current_bet = state["current_bet"]
    player_bet = state["bets"][name]
    to_call = current_bet - player_bet

    if action_type == "fold":
        state["folded"].append(name)
        state["acted"].append(name)

        if poker_finish_hand(state):
            return

    elif action_type == "check":
        if to_call > 0:
            raise HTTPException(400, "You must call or fold")

        state["acted"].append(name)

    elif action_type == "call":
        if to_call <= 0:
            raise HTTPException(400, "Nothing to call")

        amount = min(to_call, state["chips"][name])

        state["chips"][name] -= amount
        state["bets"][name] += amount
        state["pot"] += amount
        state["acted"].append(name)

    elif action_type == "raise":
        amount = data.get("amount", 0)

        if (
            not isinstance(amount, int)
            or isinstance(amount, bool)
            or amount <= 0
        ):
            raise HTTPException(400, "Invalid raise amount")

        total_needed = to_call + amount

        if total_needed > state["chips"][name]:
            raise HTTPException(400, "Not enough chips")

        state["chips"][name] -= total_needed
        state["bets"][name] += total_needed
        state["pot"] += total_needed
        state["current_bet"] = state["bets"][name]
        state["acted"] = [name]

    else:
        raise HTTPException(400, "Unknown poker action")

    active = [
        player for player in state["players_order"]
        if player not in state["folded"]
    ]

    if len(active) == 1:
        poker_finish_hand(state)
        return

    players_to_act = [
        player for player in active
        if state["chips"][player] > 0
    ]

    all_matched = all(
        state["bets"][player] == state["current_bet"]
        for player in players_to_act
    )

    all_acted = all(
        player in state["acted"]
        for player in players_to_act
    )

    if all_matched and all_acted:
        if not players_to_act:
            poker_showdown(state)
            return

        poker_advance_phase(state)

        if state["phase"] == "finished":
            return

    else:
        next_player = poker_next_active(state, name)

        if next_player is None:
            poker_showdown(state)
            return

        state["turn"] = next_player

    state["message"] = f"{name} performed {action_type}"

# ============================================================
# PRIVATE ROOM VIEW
# ============================================================

def room_view(room, name):
    result = public_room(room)
    state = room["state"]

    if room["game"] == "mafia":
        role = state.get("roles", {}).get(name)

        result["state"].update({
            "my_role": role,
            "my_alive": name in state.get("alive", []),
            "my_action_done": name in state.get("action_done", []),
            "my_check": state.get("private_checks", {}).get(name),
            "mafia_teammates": [
                player
                for player in state.get("alive", [])
                if (
                    player != name
                    and state.get("roles", {}).get(player) == "mafia"
                )
            ] if role == "mafia" else [],
            "my_vote": state.get("votes", {}).get(name),
        })

    elif room["game"] == "durak":
        result["state"].update({
            "my_hand": state.get("hands", {}).get(name, []),
            "passed": name in state.get("passed", []),
        })

    elif room["game"] == "poker":
        result["state"].update({
            "my_hand": state.get("hands", {}).get(name, []),
            "my_bet": state.get("bets", {}).get(name, 0),
            "my_chips": state.get("chips", {}).get(name, 0),
            "my_folded": name in state.get("folded", []),
        })

    return result


# ============================================================
# WEBSOCKET BROADCAST
# ============================================================

async def broadcast(code):
    room = rooms.get(code)

    if not room:
        return

    sockets = connections.get(code, {})

    disconnected = []

    for name, websocket in list(sockets.items()):
        try:
            await websocket.send_json({
                "type": "update",
                "room": room_view(room, name),
            })
        except Exception:
            disconnected.append(name)

    for name in disconnected:
        sockets.pop(name, None)

    if not sockets:
        connections.pop(code, None)


async def send_error(websocket, message):
    try:
        await websocket.send_json({
            "type": "error",
            "message": str(message),
        })
    except Exception:
        pass


# ============================================================
# START GAME
# ============================================================

@app.post("/api/rooms/{code}/start")
async def start_game(code: str, data: dict):
    room = get_room(code)

    name = clean_name(str(data.get("name", "")))

    if name != room["host"]:
        raise HTTPException(403, "Only host can start the game")

    if room["started"]:
        raise HTTPException(400, "Game already started")

    game = room["game"]

    if game == "ttt":
        start_ttt(room)

    elif game == "crocodile":
        start_crocodile(room)

    elif game == "mafia":
        start_mafia(room)

    elif game == "durak":
        start_durak(room)

    elif game == "poker":
        start_poker(room)

    else:
        raise HTTPException(400, "Unknown game")

    await broadcast(code)

    return {
        "success": True,
        "room": public_room(room),
    }


# ============================================================
# WEBSOCKET
# ============================================================

@app.websocket("/ws/{code}")
async def websocket_room(
    websocket: WebSocket,
    code: str,
    name: str,
):
    code = code.upper().strip()

    room = rooms.get(code)

    if not room:
        await websocket.accept()
        await send_error(websocket, "Room not found")
        await websocket.close()
        return

    name = " ".join(name.strip().split())

    if name not in room["players"]:
        await websocket.accept()
        await send_error(websocket, "You are not a member of this room")
        await websocket.close()
        return

    await websocket.accept()

    connections.setdefault(code, {})
    connections[code][name] = websocket

    try:
        await websocket.send_json({
            "type": "update",
            "room": room_view(room, name),
        })

        while True:
            data = await websocket.receive_json()

            if not isinstance(data, dict):
                await send_error(websocket, "Invalid message")
                continue

            action = data.get("action")

            try:
                room = rooms.get(code)

                if not room:
                    await send_error(websocket, "Room no longer exists")
                    break

                # ------------------------------------------------
                # TIC TAC TOE
                # ------------------------------------------------

                if action == "move":
                    if room["game"] != "ttt":
                        raise HTTPException(400, "Wrong game")

                    ttt_move(
                        room,
                        name,
                        data.get("index"),
                    )

                # ------------------------------------------------
                # CROCODILE DENTIST
                # ------------------------------------------------

                elif action == "crocodile_press":
                    if room["game"] != "crocodile":
                        raise HTTPException(400, "Wrong game")

                    crocodile_press(
                        room,
                        name,
                        data.get("tooth"),
                    )

                # ------------------------------------------------
                # MAFIA
                # ------------------------------------------------

                elif action in ("mafia", "vote"):
                    if room["game"] != "mafia":
                        raise HTTPException(400, "Wrong game")

                    if action == "vote":
                        mafia_action(
                            room,
                            name,
                            {
                                "type": "vote",
                                "target": data.get("target"),
                            },
                        )
                    else:
                        mafia_action(
                            room,
                            name,
                            data,
                        )

                # ------------------------------------------------
                # DURAK
                # ------------------------------------------------

                elif action == "durak":
                    if room["game"] != "durak":
                        raise HTTPException(400, "Wrong game")

                    durak_action(
                        room,
                        name,
                        data,
                    )

                # ------------------------------------------------
                # POKER
                # ------------------------------------------------

                elif action == "poker":
                    if room["game"] != "poker":
                        raise HTTPException(400, "Wrong game")

                    poker_action(
                        room,
                        name,
                        data,
                    )

                # ------------------------------------------------
                # UNKNOWN ACTION
                # ------------------------------------------------

                else:
                    raise HTTPException(400, "Unknown action")

                await broadcast(code)

            except HTTPException as error:
                await send_error(
                    websocket,
                    error.detail,
                )

            except Exception as error:
                print(
                    f"ZENTRA game error in {code}: {error}"
                )

                await send_error(
                    websocket,
                    "Internal game error",
                )

    except WebSocketDisconnect:
        pass

    except Exception as error:
        print(f"ZENTRA WebSocket error: {error}")

    finally:
        current = connections.get(code, {})

        if current.get(name) is websocket:
            current.pop(name, None)

        if not current:
            connections.pop(code, None)


# ============================================================
# SERVER STARTUP / SHUTDOWN
# ============================================================

@app.on_event("startup")
async def startup_event():
    print("ZENTRA API started")


@app.on_event("shutdown")
async def shutdown_event():
    for sockets in list(connections.values()):
        for websocket in list(sockets.values()):
            try:
                await websocket.close()
            except Exception:
                pass

    connections.clear()

    print("ZENTRA API stopped")
