import os, json, random, secrets, asyncio
from typing import Dict, Any, List
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="ZENTRA ONLINE")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# In-memory rooms: suitable for the first test deployment. Rooms reset when the free server restarts.
rooms: Dict[str, Dict[str, Any]] = {}
sockets: Dict[str, List[WebSocket]] = {}
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
        return {"board": [""] * 9, "turn": "X", "winner": None, "moves": 0}
    if game == "mafia":
        return {"phase": "lobby", "roles": {}, "votes": {}, "alive": [], "night_actions": {}, "winner": None, "round": 0}
    if game == "durak":
        deck = [(r, s) for s in ["♠", "♥", "♦", "♣"] for r in ["6","7","8","9","10","J","Q","K","A"]]
        random.shuffle(deck)
        hands = {}
        return {"deck": deck, "hands": hands, "trump": None, "table": [], "attacker": 0, "defender": 1, "phase": "lobby", "winner": None}
    if game == "poker":
        return {"phase": "lobby", "deck": [], "community": [], "hands": {}, "bets": {}, "pot": 0, "turn": 0, "winner": None}

def public_room(room):
    return {"code": room["code"], "game": room["game"], "host": room["host"],
            "players": len(room["players"]), "player_names": [p["name"] for p in room["players"]],
            "max_players": room["max_players"], "private": room["private"], "status": room["status"]}

def public_state(room, viewer=None):
    state = json.loads(json.dumps(room["state"]))
    game = room["game"]
    if game == "durak":
        state.pop("deck", None)
        if viewer is not None:
            state["my_hand"] = room["state"]["hands"].get(viewer, [])
        state["hand_counts"] = {k: len(v) for k,v in room["state"]["hands"].items()}
        state.pop("hands", None)
    elif game == "poker":
        state.pop("deck", None)
        state["my_hand"] = room["state"]["hands"].get(viewer, []) if viewer else []
        state.pop("hands", None)
    elif game == "mafia":
        state["my_role"] = room["state"]["roles"].get(viewer) if viewer else None
        state.pop("roles", None)
    return {"room": public_room(room), "state": state}

async def broadcast(code):
    room = rooms.get(code)
    if not room: return
    dead = []
    for ws in sockets.get(code, []):
        try:
            # Identify connection's player name
            name = getattr(ws, "zentra_name", None)
            await ws.send_json({"type": "state", **public_state(room, name)})
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in sockets.get(code, []): sockets[code].remove(ws)

def start_game(room):
    game, players = room["game"], room["players"]
    if game == "ttt":
        if len(players) != 2: raise ValueError("Хрестики-нулики — рівно 2 гравці.")
        room["state"] = fresh_game("ttt")
    elif game == "mafia":
        if len(players) < 4: raise ValueError("Для Мафії потрібно щонайменше 4 гравці.")
        names = [p["name"] for p in players]
        roles = ["mafia"] + ["doctor", "detective"] + ["citizen"] * (len(names)-3)
        random.shuffle(roles)
        st = fresh_game("mafia"); st.update({"phase":"night", "roles":dict(zip(names,roles)), "alive":names, "round":1})
        room["state"] = st
    elif game == "durak":
        if len(players) < 2 or len(players) > 6: raise ValueError("У Дурня потрібно від 2 до 6 гравців.")
        st = fresh_game("durak")
        deck = st["deck"]; names = [p["name"] for p in players]
        for name in names: st["hands"][name] = [deck.pop() for _ in range(6)]
        st["trump"] = deck[-1] if deck else None
        st.update({"phase":"playing", "attacker":0, "defender":1})
        room["state"] = st
    elif game == "poker":
        if len(players) < 2 or len(players) > 8: raise ValueError("У покер потрібно від 2 до 8 гравців.")
        deck = [(r,s) for s in ["♠","♥","♦","♣"] for r in ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]]
        random.shuffle(deck); names=[p["name"] for p in players]
        st=fresh_game("poker"); st.update({"phase":"preflop","deck":deck,"community":[],"hands":{n:[deck.pop(),deck.pop()] for n in names},"bets":{n:0 for n in names},"pot":0,"turn":0,"winner":None})
        room["state"]=st
    room["status"] = "playing"

@app.get("/")
def home(): return {"name":"ZENTRA","status":"online","realtime":True}

@app.get("/api/rooms")
def list_rooms(game: str = ""):
    return [public_room(r) for r in rooms.values() if not r["private"] and (not game or r["game"] == game)]

@app.post("/api/rooms")
def create_room(data: CreateRoom):
    if data.game not in GAMES: raise HTTPException(400, "Невідома гра")
    code = secrets.token_hex(3).upper()
    room={"code":code,"game":data.game,"host":data.player_name,"private":data.private,
          "max_players":data.max_players,"players":[{"name":data.player_name}],
          "status":"waiting","state":fresh_game(data.game)}
    rooms[code]=room; sockets[code]=[]; locks[code]=asyncio.Lock()
    return {"success":True,"code":code}

@app.post("/api/rooms/join")
def join_room(data: JoinRoom):
    code=data.code.upper().strip(); room=rooms.get(code)
    if not room: raise HTTPException(404,"Кімнату не знайдено")
    if room["status"] == "playing": raise HTTPException(400,"Гра вже почалася")
    if any(p["name"] == data.player_name for p in room["players"]): return {"success":True,"code":code}
    if len(room["players"]) >= room["max_players"]: raise HTTPException(400,"Кімната заповнена")
    room["players"].append({"name":data.player_name})
    return {"success":True,"code":code}

@app.get("/api/rooms/{code}")
def room_info(code: str):
    room=rooms.get(code.upper())
    if not room: raise HTTPException(404,"Кімнату не знайдено")
    return public_room(room)

@app.post("/api/rooms/{code}/start")
async def start(code: str, payload: dict):
    code=code.upper(); room=rooms.get(code)
    if not room: raise HTTPException(404,"Кімнату не знайдено")
    try: start_game(room)
    except ValueError as e: raise HTTPException(400,str(e))
    await broadcast(code)
    return {"success":True}

@app.websocket("/ws/{code}")
async def websocket_room(ws: WebSocket, code: str, name: str):
    code=code.upper(); room=rooms.get(code)
    await ws.accept()
    if not room:
        await ws.send_json({"type":"error","message":"Кімнату не знайдено"}); await ws.close(); return
    if name not in [p["name"] for p in room["players"]]:
        await ws.send_json({"type":"error","message":"Спочатку приєднайся до кімнати"}); await ws.close(); return
    ws.zentra_name=name; sockets.setdefault(code,[]).append(ws)
    await ws.send_json({"type":"state",**public_state(room,name)})
    try:
        while True:
            msg=await ws.receive_json()
            async with locks[code]:
                apply_action(room,name,msg)
                await broadcast(code)
    except WebSocketDisconnect:
        if ws in sockets.get(code,[]): sockets[code].remove(ws)

def apply_action(room, name, msg):
    st=room["state"]; game=room["game"]; action=msg.get("action")
    names=[p["name"] for p in room["players"]]
    if room["status"] != "playing": raise ValueError("Гра ще не почалася")
    if game == "ttt":
        if st["winner"]: return
        if name != names[0 if st["turn"]=="X" else 1]: raise ValueError("Зараз не твій хід")
        i=int(msg.get("index",-1))
        if i<0 or i>8 or st["board"][i]: raise ValueError("Ця клітинка недоступна")
        st["board"][i]=st["turn"]; st["moves"]+=1
        lines=[(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
        st["winner"]=st["turn"] if any(st["board"][a] and st["board"][a]==st["board"][b]==st["board"][c] for a,b,c in lines) else ("draw" if st["moves"]==9 else None)
        st["turn"]="O" if st["turn"]=="X" else "X"
    elif game == "mafia":
        if st["phase"] not in ("night","day"): raise ValueError("Невірна фаза")
        if name not in st["alive"]: raise ValueError("Ти вибув")
        target=msg.get("target")
        if target not in st["alive"]: raise ValueError("Невірна ціль")
        if st["phase"]=="night":
            role=st["roles"][name]
            allowed={"mafia":"kill","doctor":"heal","detective":"check"}
            if role not in allowed or action != allowed[role]: raise ValueError("Твоя роль не може виконати цю дію")
            st["night_actions"][role]=target
            if len(st["night_actions"])>=3:
                killed=st["night_actions"].get("kill")
                if killed and killed != st["night_actions"].get("heal"): st["alive"].remove(killed)
                st["night_actions"]={}; st["phase"]="day"
        else:
            st["votes"][name]=target
            if len(st["votes"])==len(st["alive"]):
                tally={}
                for v in st["votes"].values(): tally[v]=tally.get(v,0)+1
                out=max(tally,key=tally.get); st["alive"].remove(out)
                st["votes"]={}; st["phase"]="night"; st["round"]+=1
        mafia_alive=sum(1 for n in st["alive"] if st["roles"][n]=="mafia")
        town_alive=len(st["alive"])-mafia_alive
        if mafia_alive==0: st["winner"]="town"; st["phase"]="finished"
        elif mafia_alive>=town_alive: st["winner"]="mafia"; st["phase"]="finished"
    elif game == "durak":
        if st["phase"]!="playing": raise ValueError("Гра не активна")
        if action=="attack":
            card=tuple(msg.get("card",[]))
            if name != names[st["attacker"]] or card not in st["hands"][name]: raise ValueError("Не твоя карта або не твій хід")
            if st["table"] and card[0] not in [x[0] for pair in st["table"] for x in pair if x]: raise ValueError("Можна підкидати лише карту того самого рангу")
            st["hands"][name].remove(list(card)); st["table"].append([list(card),None])
        elif action=="take":
            if name != names[st["defender"]]: raise ValueError("Не твій хід")
            for a,b in st["table"]:
                st["hands"][name].append(a)
                if b: st["hands"][name].append(b)
            st["table"]=[]; st["attacker"],st["defender"]=st["defender"],st["attacker"]
        elif action=="pass":
            if name != names[st["attacker"]]: raise ValueError("Не твій хід")
            st["table"]=[]
            st["attacker"]=(st["attacker"]+1)%len(names); st["defender"]=(st["attacker"]+1)%len(names)
        else: raise ValueError("Невідома дія")
        for n in names:
            while len(st["hands"][n])<6 and st["deck"]: st["hands"][n].append(list(st["deck"].pop()))
        remaining=[n for n in names if st["hands"][n]]
        if len(remaining)<=1 and not st["deck"]:
            st["winner"]=remaining[0] if remaining else "draw"; st["phase"]="finished"
    elif game == "poker":
        if st["phase"]=="finished": return
        if action not in ("check","call","fold","raise"): raise ValueError("Невідома дія")
        if action=="fold":
            st["winner"]=next((n for n in names if n!=name),"draw"); st["phase"]="finished"
        else:
            if action=="raise":
                amount=max(1,min(int(msg.get("amount",10)),1000))
                st["bets"][name]+=amount; st["pot"]+=amount
            if action=="call":
                highest=max(st["bets"].values()); diff=highest-st["bets"][name]
                st["bets"][name]+=diff; st["pot"]+=diff
            st["turn"]=(st["turn"]+1)%len(names)
            if st["turn"]==0:
                if st["phase"]=="preflop":
                    st["community"]=[st["deck"].pop() for _ in range(3)]; st["phase"]="flop"
                elif st["phase"]=="flop":
                    st["community"].append(st["deck"].pop()); st["phase"]="turn"
                elif st["phase"]=="turn":
                    st["community"].append(st["deck"].pop()); st["phase"]="river"
                else:
                    st["winner"]=random.choice(names); st["phase"]="finished"
    else: raise ValueError("Невідома гра")

@app.get("/health")
def health(): return {"ok":True,"rooms":len(rooms)}
