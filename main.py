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

                if name not in room["players"]:
                    continue

                player_index = room["players"].index(name)

                symbol = (
                    "X" if player_index == 0 else "O"
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
                crocodile_press(room, name, data.get("tooth"))
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

    except WebSocketDisconnect:
        pass

    except Exception as error:
        print("WebSocket error:", error)

    finally:
        connections.get(code, {}).pop(ws, None)
