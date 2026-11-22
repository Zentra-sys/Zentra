
import os
import json
import uuid
import random
import sqlite3
import asyncio
from datetime import date, timedelta
from typing import Optional

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
                status_code=409,
                detail="Цей нікнейм уже зайнятий! Обери інший."
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

    uid = data.uid.strip()
    name = data.name.strip()[:24]
    if not uid or not name:
        raise HTTPException(400, "Вкажи ім'я")
    with db() as con:
        con.execute(
            "INSERT INTO users(uid,name) VALUES(?,?) "
            "ON CONFLICT(uid) DO UPDATE SET name=excluded.name",
            (uid, name)
        )
        user = con.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
    return dict(user)

@app.get("/api/profile/{uid}")
def get_profile(uid: str):
    with db() as con:
        user = con.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
    if not user:
        raise HTTPException(404, "Профіль не знайдено")
    return dict(user)

@app.post("/api/daily/{uid}")
def daily(uid: str):
    today = date.today()
    with db() as con:
        user = con.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
        if not user:
            raise HTTPException(404, "Профіль не знайдено")

        last = date.fromisoformat(user["last_claim"]) if user["last_claim"] else None

        if last == today:
            return {
                "claimed": False,
                "message": "Сьогодні нагороду вже отримано",
                "points": user["points"],
                "streak": user["streak"]
            }

        if last == today - timedelta(days=1):
            streak = user["streak"] + 1
        else:
            streak = 1

        reward = 10 if streak == 1 else 50 if streak == 7 else 10

        con.execute(
            "UPDATE users SET points=points+?, last_claim=?, streak=? WHERE uid=?",
            (reward, today.isoformat(), streak, uid)
        )
        updated = con.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()

    return {
        "claimed": True,
        "reward": reward,
        "points": updated["points"],
        "streak": updated["streak"],
        "message": f"Отримано {reward} балів!"
    }

@app.post("/api/rooms")
def create_room(data: RoomCreate):
    code = str(random.randint(100000, 999999))
    while code in rooms:
        code = str(random.randint(100000, 999999))

    rooms[code] = {
        "code": code,
        "game": data.game,
        "host": data.name,
        "players": [data.name],
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
    if data.name not in room["players"]:
        room["players"].append(data.name)
    return room

@app.get("/api/rooms/{code}")
def room_info(code: str):
    room = rooms.get(code)
    if not room:
        raise HTTPException(404, "Кімнату не знайдено")
    return room

@app.post("/api/rooms/{code}/start")
async def start_room(code: str):
    room = rooms.get(code)
    if not room:
        raise HTTPException(404, "Кімнату не знайдено")
    if len(room["players"]) < 2:
        raise HTTPException(400, "Потрібно щонайменше 2 гравці")
    room["started"] = True
    room["state"] = {"turn": 0, "board": [""] * 9, "winner": None}
    await broadcast(code)
    return room

async def broadcast(code):
    room = rooms.get(code)
    if not room:
        return
    dead = []
    for ws in connections.get(code, []):
        try:
            await ws.send_json(room)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connections[code].remove(ws)

@app.websocket("/ws/{code}")
async def websocket_room(ws: WebSocket, code: str, name: str = "Гравець"):
    await ws.accept()
    if code not in rooms:
        await ws.close(code=1008)
        return
    connections.setdefault(code, []).append(ws)
    try:
        await ws.send_json(rooms[code])
        while True:
            data = await ws.receive_json()
            room = rooms.get(code)
            if not room:
                break

            if data.get("action") == "move" and room["game"] == "ttt":
                state = room["state"]
                index = data.get("index")
                if not isinstance(index, int) or not 0 <= index < 9:
                    continue
                board = state.get("board", [""] * 9)
                if board[index] or state.get("winner"):
                    continue

                player_index = room["players"].index(name) if name in room["players"] else 0
                symbol = "X" if player_index == 0 else "O"
                current = "X" if state.get("turn", 0) % 2 == 0 else "O"
                if symbol != current:
                    continue

                board[index] = symbol
                state["turn"] = state.get("turn", 0) + 1

                wins = [
                    (0,1,2),(3,4,5),(6,7,8),
                    (0,3,6),(1,4,7),(2,5,8),
                    (0,4,8),(2,4,6)
                ]
                for a,b,c in wins:
                    if board[a] and board[a] == board[b] == board[c]:
                        state["winner"] = board[a]
                        break

                if not state["winner"] and all(board):
                    state["winner"] = "draw"

                if state["winner"] and not state.get("rewarded"):
                    state["rewarded"] = True
                    if state["winner"] == "draw":
                        reward = 5
                        for player in room["players"]:
                            with db() as con:
                                con.execute("UPDATE users SET points=points+? WHERE name=?", (reward, player))
                    else:
                        winner_index = 0 if state["winner"] == "X" else 1
                        if winner_index < len(room["players"]):
                            winner_name = room["players"][winner_index]
                            with db() as con:
                                con.execute("UPDATE users SET points=points+30 WHERE name=?", (winner_name,))

                await broadcast(code)

    except WebSocketDisconnect:
        pass
    finally:
        if ws in connections.get(code, []):
            connections[code].remove(ws)
