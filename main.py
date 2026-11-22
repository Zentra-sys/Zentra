
import os
import random
import sqlite3
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
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
            points INTEGER DEFAULT 0,
            last_claim TEXT,
            streak INTEGER DEFAULT 0
        )
        """)


init_db()


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


@app.get("/")
def home():
    return {"name": "ZENTRA", "status": "online"}


@app.get("/health")
def health():
    return {"ok": True, "rooms": len(rooms)}


@app.post("/api/profile")
def profile(data: Profile):
    uid = data.uid.strip()
    name = data.name.strip()[:24]

    if not uid or not name:
        raise HTTPException(400, "Вкажи нікнейм")

    with db() as con:
        existing = con.execute(
            "SELECT uid FROM users WHERE lower(name)=lower(?) AND uid!=?",
            (name, uid)
        ).fetchone()

        if existing:
            raise HTTPException(
                409,
                "Цей нікнейм уже зайнятий! Обери інший."
            )

        con.execute(
            "INSERT INTO users(uid,name) VALUES(?,?) "
            "ON CONFLICT(uid) DO UPDATE SET name=excluded.name",
            (uid, name)
        )

        user = con.execute(
            "SELECT * FROM users WHERE uid=?",
            (uid,)
        ).fetchone()

    return dict(user)


@app.get("/api/profile/{uid}")
def get_profile(uid: str):
    with db() as con:
        user = con.execute(
            "SELECT * FROM users WHERE uid=?", (uid,)
        ).fetchone()

    if not user:
        raise HTTPException(404, "Профіль не знайдено")

    return dict(user)


@app.post("/api/daily/{uid}")
def daily(uid: str):
    today = date.today()

    with db() as con:
        user = con.execute(
            "SELECT * FROM users WHERE uid=?", (uid,)
        ).fetchone()

        if not user:
            raise HTTPException(404, "Профіль не знайдено")

        last = (
            date.fromisoformat(user["last_claim"])
            if user["last_claim"] else None
        )

        if last == today:
            return {
                "claimed": False,
                "message": "Сьогодні нагороду вже отримано",
                "points": user["points"],
                "streak": user["streak"]
            }

        streak = (
            user["streak"] + 1
            if last == today - timedelta(days=1)
            else 1
        )

        reward = 50 if streak == 7 else 10

        con.execute(
            "UPDATE users SET points=points+?, last_claim=?, streak=? "
            "WHERE uid=?",
            (reward, today.isoformat(), streak, uid)
        )

        updated = con.execute(
            "SELECT * FROM users WHERE uid=?", (uid,)
        ).fetchone()

    return {
        "claimed": True,
        "reward": reward,
        "points": updated["points"],
        "streak": updated["streak"],
        "message": f"Отримано {reward} балів!"
    }


def new_code():
    code = str(random.randint(100000, 999999))
    while code in rooms:
        code = str(random.randint(100000, 999999))
    return code


@app.post("/api/rooms")
def create_room(data: RoomCreate):
    game = data.game.lower()

    if game not in ("ttt", "durak", "mafia", "poker"):
        raise HTTPException(400, "Невідомий тип гри")

    if not data.name.strip():
        raise HTTPException(400, "Вкажи ім'я")

    code = new_code()

    rooms[code] = {
        "code": code,
        "game": game,
        "host": data.name.strip(),
        "players": [data.name.strip()],
        "open": data.open,
        "started": False,
        "state": {}
    }

    return rooms[code]


@app.get("/api/rooms")
def get_rooms():
    return list(rooms.values())


@app.get("/api/lobbies")
def lobbies():
    return [
        room for room in rooms.values()
        if room["open"] and not room["started"]
    ]


@app.post("/api/rooms/join")
def join_room(data: JoinRoom):
    room = rooms.get(data.code)

    if not room:
        raise HTTPException(404, "Кімнату не знайдено")

    if room["started"]:
        raise HTTPException(400, "Гра вже почалася")

    name = data.name.strip()

    if not name:
        raise HTTPException(400, "Вкажи ім'я")

    if name not in room["players"]:
        room["players"].append(name)

    return room


@app.get("/api/rooms/{code}")
def room_info(code: str):
    room = rooms.get(code)

    if not room:
        raise HTTPException(404, "Кімнату не знайдено")

    return room


def award_players(names, points):
    with db() as con:
        for name in set(names):
            con.execute(
                "UPDATE users SET points=points+? WHERE name=?",
                (points, name)
            )


def mafia_roles(players):
    count = len(players)
    shuffled = players[:]
    random.shuffle(shuffled)

    mafia_count = max(1, count // 4)

    roles = {}
    for name in shuffled[:mafia_count]:
        roles[name] = "mafia"

    remaining = shuffled[mafia_count:]

    if remaining:
        doctor = remaining.pop()
        roles[doctor] = "doctor"

    if count >= 5 and remaining:
        detective = remaining.pop()
        roles[detective] = "detective"

    for name in remaining:
        roles[name] = "citizen"

    return roles


def start_mafia(room):
    players = room["players"]

    if len(players) < 4:
        raise HTTPException(
            400,
            "Для Мафії потрібно щонайменше 4 гравці"
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


def start_ttt(room):
    room["started"] = True
    room["state"] = {
        "turn": 0,
        "board": [""] * 9,
        "winner": None
    }


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
        if len(room["players"]) < 2:
            raise HTTPException(400, "Потрібно щонайменше 2 гравці")
        start_ttt(room)
    else:
        raise HTTPException(
            400,
            "Ця гра ще не має готової механіки"
        )

    await broadcast(code)
    return room


def mafia_finish(room):
    state = room["state"]
    alive = state["alive"]
    roles = state["roles"]

    mafia_alive = [
        name for name in alive if roles[name] == "mafia"
    ]
    town_alive = [
        name for name in alive if roles[name] != "mafia"
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
        winners = [
            name for name in alive
            if (
                roles[name] == "mafia"
                if state["winner"] == "mafia"
                else roles[name] != "mafia"
            )
        ]
        award_players(winners, 30)
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
        if roles.get(name) == "mafia"
        and action.get("type") == "kill"
        and action.get("target") in alive
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
            if roles.get(name) == "doctor"
            and action.get("type") == "save"
        ),
        None
    )

    if victim and victim == doctor_target:
        state["last_result"] = (
            "Лікар врятував гравця. Ніхто не вибув цієї ночі."
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
                name for name, count in counts.items()
                if count == highest
            ]
            eliminated = (
                candidates[0] if len(candidates) == 1 else None
            )

        if eliminated:
            alive.remove(eliminated)
            state["last_result"] = (
                f"За результатами голосування вибув: {eliminated}."
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

        if target not in alive:
            return

        if target == name:
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
            player for player in alive
            if roles[player] in ("mafia", "doctor", "detective")
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

        if all(player in state["votes"] for player in alive):
            resolve_day(room)


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

    if room["game"] == "mafia" and room["started"]:
        state = room["state"]
        role = state["roles"].get(name)

        public = {
            "phase": state["phase"],
            "round": state["round"],
            "alive": state["alive"],
            "winner": state["winner"],
            "last_result": state["last_result"],
            "eliminated": state["eliminated"],
            "votes_count": len(state["votes"]),
            "players_count": len(state["alive"])
        }

        result["state"] = public
        result["public_state"] = public
        result["my_role"] = role
        result["my_alive"] = name in state["alive"]

        result["my_action_done"] = (
            name in state["night_actions"]
            if state["phase"] == "night"
            else name in state["votes"]
        )

        result["my_check"] = state["private_checks"].get(name)

        result["mafia_teammates"] = (
            [
                player for player in state["alive"]
                if state["roles"][player] == "mafia"
            ]
            if role == "mafia" else []
        )

    return result
    result = {
        "code": room["code"],
        "game": room["game"],
        "host": room["host"],
        "players": room["players"],
        "open": room["open"],
        "started": room["started"],
        "state": room["state"]
    }

    if room["game"] == "mafia" and room["started"]:
        state = room["state"]
        role = state["roles"].get(name)

        result["my_role"] = role
        result["my_alive"] = name in state["alive"]
        result["my_action_done"] = (
            name in state["night_actions"]
            if state["phase"] == "night"
            else name in state["votes"]
        )
        result["my_check"] = state["private_checks"].get(name)

        result["mafia_teammates"] = (
            [
                player for player in state["alive"]
                if state["roles"][player] == "mafia"
            ]
            if role == "mafia" else []
        )

        result["public_state"] = {
            "phase": state["phase"],
            "round": state["round"],
            "alive": state["alive"],
            "winner": state["winner"],
            "last_result": state["last_result"],
            "eliminated": state["eliminated"],
            "votes_count": len(state["votes"]),
            "players_count": len(state["alive"])
        }

    return result


async def broadcast(code):
    room = rooms.get(code)

    if not room:
        return

    dead = []

    for ws, name in list(connections.get(code, {}).items()):
        try:
            await ws.send_json(room_view(room, name))
        except Exception:
            dead.append(ws)

    for ws in dead:
        connections.get(code, {}).pop(ws, None)


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
        await ws.send_json(room_view(room, name))

        while True:
            data = await ws.receive_json()
            room = rooms.get(code)

            if not room:
                break

            if data.get("action") == "move" and room["game"] == "ttt":
                state = room["state"]
                index = data.get("index")

                if (
                    not isinstance(index, int)
                    or not 0 <= index < 9
                    or not room["started"]
                ):
                    continue

                board = state.get("board", [""] * 9)

                if board[index] or state.get("winner"):
                    continue

                if name not in room["players"]:
                    continue

                player_index = room["players"].index(name)
                symbol = "X" if player_index == 0 else "O"
                current = (
                    "X" if state.get("turn", 0) % 2 == 0 else "O"
                )

                if symbol != current:
                    continue

                board[index] = symbol
                state["turn"] += 1

                wins = [
                    (0, 1, 2), (3, 4, 5), (6, 7, 8),
                    (0, 3, 6), (1, 4, 7), (2, 5, 8),
                    (0, 4, 8), (2, 4, 6)
                ]

                for a, b, c in wins:
                    if board[a] and board[a] == board[b] == board[c]:
                        state["winner"] = board[a]
                        break

                if not state["winner"] and all(board):
                    state["winner"] = "draw"

                if state["winner"] and not state.get("rewarded"):
                    state["rewarded"] = True

                    if state["winner"] == "draw":
                        award_players(room["players"], 5)
                    else:
                        winner_index = (
                            0 if state["winner"] == "X" else 1
                        )
                        if winner_index < len(room["players"]):
                            award_players(
                                [room["players"][winner_index]], 30
                            )

                await broadcast(code)

            elif (
                data.get("action") == "mafia"
                and room["game"] == "mafia"
                and room["started"]
            ):
                mafia_action(room, name, data)
                await broadcast(code)

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

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        connections.get(code, {}).pop(ws, None)
