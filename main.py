
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import sqlite3
import secrets
import json
from datetime import datetime, timezone

app = FastAPI(title="ZENTRA API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

DB = "zentra.db"

def connect():
    return sqlite3.connect(DB)

def init_db():
    with connect() as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            code TEXT PRIMARY KEY,
            game TEXT,
            host TEXT,
            private INTEGER,
            max_players INTEGER,
            players TEXT,
            created TEXT
        )
        """)

init_db()

class CreateRoom(BaseModel):
    game: str
    player_name: str = Field(min_length=1, max_length=30)
    private: bool = False
    max_players: int = Field(default=4, ge=2, le=20)

class JoinRoom(BaseModel):
    code: str
    player_name: str = Field(min_length=1, max_length=30)

@app.get("/")
def home():
    return {"name": "ZENTRA", "status": "online"}

@app.get("/api/rooms")
def rooms(game: str = ""):
    with connect() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM rooms WHERE private=0"
        ).fetchall()

    result = []
    for r in rows:
        if game and r["game"] != game:
            continue
        players = json.loads(r["players"])
        result.append({
            "code": r["code"],
            "game": r["game"],
            "host": r["host"],
            "players": len(players),
            "max_players": r["max_players"]
        })
    return result

@app.post("/api/rooms")
def create_room(data: CreateRoom):
    code = secrets.token_hex(3).upper()

    players = [data.player_name]

    with connect() as db:
        db.execute(
            """INSERT INTO rooms
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                code,
                data.game,
                data.player_name,
                int(data.private),
                data.max_players,
                json.dumps(players),
                datetime.now(timezone.utc).isoformat()
            )
        )

    return {"success": True, "code": code}

@app.post("/api/rooms/join")
def join_room(data: JoinRoom):
    code = data.code.upper().strip()

    with connect() as db:
        db.row_factory = sqlite3.Row
        room = db.execute(
            "SELECT * FROM rooms WHERE code=?",
            (code,)
        ).fetchone()

        if not room:
            raise HTTPException(404, "Кімнату не знайдено")

        players = json.loads(room["players"])

        if data.player_name in players:
            return {"success": True, "code": code}

        if len(players) >= room["max_players"]:
            raise HTTPException(400, "Кімната заповнена")

        players.append(data.player_name)

        db.execute(
            "UPDATE rooms SET players=? WHERE code=?",
            (json.dumps(players), code)
        )

    return {"success": True, "code": code}

@app.get("/api/rooms/{code}")
def get_room(code: str):
    with connect() as db:
        db.row_factory = sqlite3.Row
        room = db.execute(
            "SELECT * FROM rooms WHERE code=?",
            (code.upper(),)
        ).fetchone()

    if not room:
        raise HTTPException(404, "Кімнату не знайдено")

    return {
        "code": room["code"],
        "game": room["game"],
        "host": room["host"],
        "private": bool(room["private"]),
        "max_players": room["max_players"],
        "players": json.loads(room["players"])
    }

print("ZENTRA API створено!")
