
import asyncio
import secrets
from typing import Dict, Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="ZENTRA ONLINE")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

rooms: Dict[str, Dict[str, Any]] = {}
sockets: Dict[str, list] = {}
locks: Dict[str, asyncio.Lock] = {}

GAMES = {"ttt", "mafia", "durak", "poker"}


class CreateRoom(BaseModel):
    game: str
    player_name: str = Field(min_length=1, max_length=30)
    private: bool = False
    max_players: int = Field(default=4, ge=2, le=20)


class JoinRoom(BaseModel):
    code: str
    player_name: str = Field(min_length=1, max_length=30)


def fresh_game(game):
    if game == "ttt":
        return {
            "board": [""] * 9,
            "turn": "X",
            "winner": None,
            "moves": 0
        }
    return {"phase": "lobby", "winner": None}


def public_room(room):
    return {
        "code": room["code"],
        "game": room["game"],
        "host": room["host"],
        "players": len(room["players"]),
        "player_names": [p["name"] for p in room["players"]],
        "max_players": room["max_players"],
        "private": room["private"],
        "status": room["status"]
    }


def public_state(room):
    return {
        "room": public_room(room),
        "state": room["state"]
    }


async def broadcast(code):
    room = rooms.get(code)
    if not room:
        return

    dead = []

    for ws in sockets.get(code, []):
        try:
            await ws.send_json({
                "type": "state",
                **public_state(room)
            })
        except Exception:
            dead.append(ws)

    for ws in dead:
        if ws in sockets.get(code, []):
            sockets[code].remove(ws)


@app.get("/")
def home():
    return {
        "name": "ZENTRA",
        "status": "online",
        "realtime": True
    }


@app.get("/health")
def health():
    return {"ok": True, "rooms": len(rooms)}


@app.get("/api/rooms")
def list_rooms(game: str = ""):
    return [
        public_room(room)
        for room in rooms.values()
        if not room["private"]
        and (not game or room["game"] == game)
    ]


@app.post("/api/rooms")
async def create_room(data: CreateRoom):
    if data.game not in GAMES:
        raise HTTPException(400, "Невідома гра")

    code = secrets.token_hex(3).upper()

    room = {
        "code": code,
        "game": data.game,
        "host": data.player_name,
        "private": data.private,
        "max_players": data.max_players,
        "players": [{"name": data.player_name}],
        "status": "waiting",
        "state": fresh_game(data.game)
    }

    rooms[code] = room
    sockets[code] = []
    locks[code] = asyncio.Lock()

    return {"success": True, "code": code}


@app.post("/api/rooms/join")
async def join_room(data: JoinRoom):
    code = data.code.upper().strip()
    room = rooms.get(code)

    if not room:
        raise HTTPException(404, "Кімнату не знайдено")

    if room["status"] != "waiting":
        raise HTTPException(400, "Гра вже почалася")

    if any(p["name"] == data.player_name for p in room["players"]):
        return {"success": True, "code": code}

    if len(room["players"]) >= room["max_players"]:
        raise HTTPException(400, "Кімната заповнена")

    room["players"].append({"name": data.player_name})
    await broadcast(code)

    return {"success": True, "code": code}


@app.get("/api/rooms/{code}")
def room_info(code: str):
    room = rooms.get(code.upper())

    if not room:
        raise HTTPException(404, "Кімнату не знайдено")

    return public_room(room)


@app.post("/api/rooms/{code}/start")
async def start_game(code: str, payload: dict):
    code = code.upper()
    room = rooms.get(code)

    if not room:
        raise HTTPException(404, "Кімнату не знайдено")

    if payload.get("player_name") != room["host"]:
        raise HTTPException(403, "Почати гру може тільки власник кімнати")

    if room["status"] != "waiting":
        raise HTTPException(400, "Гра вже почалася")

    if room["game"] == "ttt":
        if len(room["players"]) != 2:
            raise HTTPException(400, "Потрібно рівно 2 гравці")

        room["state"] = fresh_game("ttt")

    else:
        raise HTTPException(
            501,
            "Ця гра ще готується. Поки доступні кімнати та лобі."
        )

    room["status"] = "playing"

    await broadcast(code)

    return {"success": True}


def apply_action(room, name, msg):
    if room["status"] != "playing":
        raise ValueError("Гра ще не почалася")

    if room["game"] != "ttt":
        raise ValueError("Ця гра ще не підтримується")

    state = room["state"]

    if state["winner"]:
        raise ValueError("Гра вже завершена")

    players = [p["name"] for p in room["players"]]
    expected = players[0] if state["turn"] == "X" else players[1]

    if name != expected:
        raise ValueError("Зараз не твій хід")

    index = int(msg.get("index", -1))

    if index < 0 or index > 8 or state["board"][index]:
        raise ValueError("Недоступна клітинка")

    state["board"][index] = state["turn"]
    state["moves"] += 1

    lines = [
        (0,1,2), (3,4,5), (6,7,8),
        (0,3,6), (1,4,7), (2,5,8),
        (0,4,8), (2,4,6)
    ]

    for a, b, c in lines:
        if (
            state["board"][a]
            and state["board"][a] == state["board"][b]
            and state["board"][b] == state["board"][c]
        ):
            state["winner"] = state["turn"]
            break

    if not state["winner"] and state["moves"] == 9:
        state["winner"] = "draw"

    state["turn"] = "O" if state["turn"] == "X" else "X"


@app.websocket("/ws/{code}")
async def websocket_room(ws: WebSocket, code: str, name: str):
    code = code.upper()
    await ws.accept()

    room = rooms.get(code)

    if not room:
        await ws.send_json({
            "type": "error",
            "message": "Кімнату не знайдено"
        })
        await ws.close()
        return

    if name not in [p["name"] for p in room["players"]]:
        await ws.send_json({
            "type": "error",
            "message": "Спочатку приєднайся до кімнати"
        })
        await ws.close()
        return

    ws.zentra_name = name
    sockets.setdefault(code, []).append(ws)

    await ws.send_json({
        "type": "state",
        **public_state(room)
    })

    await broadcast(code)

    try:
        while True:
            message = await ws.receive_json()

            try:
                async with locks[code]:
                    apply_action(room, name, message)

                await broadcast(code)

            except (ValueError, TypeError, IndexError) as error:
                await ws.send_json({
                    "type": "error",
                    "message": str(error)
                })

    except WebSocketDisconnect:
        pass

    finally:
        if ws in sockets.get(code, []):
            sockets[code].remove(ws)
        await broadcast(code)
