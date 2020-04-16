import os
import random
from datetime import datetime
from typing import List

from kivy.clock import Clock

from game_node import GameNode
from sgf_parser import SGF, Move


class IllegalMoveException(Exception):
    pass


class KaTrainSGF(SGF):
    _NODE_CLASS = GameNode


class Game:
    """Represents a game of go, including an implementation of capture rules."""

    GAME_COUNTER = 0

    def __init__(self, katrain, engine, config, board_size=None, move_tree=None):
        Game.GAME_COUNTER += 1
        self.katrain = katrain
        self.engine = engine
        self.config = config
        self.game_id = datetime.strftime(datetime.now(), "%Y-%m-%d %H %M %S")

        if move_tree:
            self.root = move_tree
            self.board_size = self.root.board_size
            self.komi = self.root.komi
            handicap = self.root.get_first("HA")
            if handicap is not None and not self.root.placements:
                self.place_handicap_stones(handicap)
        else:
            self.board_size = board_size or config['size']
            self.komi = self.config.get(f"komi_{self.board_size}",6.5)
            self.root = GameNode(properties={"GM":1,"FF":4,"RU": "JP", "SZ":  self.board_size, "KM": self.komi,
                                             "AP": "KaTrain:https://github.com/sanderland/katrain", "DT": self.game_id})

        self.current_node = self.root
        self._init_chains()

        def analyze_game(_dt):
            self.engine.on_new_game()
            for node in self.root.nodes_in_tree:
                node.analyze(self.engine,priority=-1_000_000)
        Clock.schedule_once(analyze_game, -1)  # return faster

    # -- move tree functions --
    def _init_chains(self):
        self.board = [[-1 for _x in range(self.board_size)] for _y in range(self.board_size)]  # type: List[List[int]]  #  board pos -> chain id
        self.chains = []  # type: List[List[Move]]  #   chain id -> chain
        self.prisoners = []  # type: List[Move]
        self.last_capture = []  # type: List[Move]
        try:
            #            for m in self.moves:
            for node in self.current_node.nodes_from_root:
                for m in node.move_with_placements:
                    self._validate_move_and_update_chains(m, True)  # ignore ko since we didn't know if it was forced
        except IllegalMoveException as e:
            raise Exception(f"Unexpected illegal move ({str(e)})")

    def _validate_move_and_update_chains(self, move: Move, ignore_ko: bool):
        def neighbours(moves):
            return {
                self.board[m.coords[1] + dy][m.coords[0] + dx]
                for m in moves
                for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]
                if 0 <= m.coords[0] + dx < self.board_size and 0 <= m.coords[1] + dy < self.board_size
            }

        ko_or_snapback = len(self.last_capture) == 1 and self.last_capture[0] == move
        self.last_capture = []

        if move.is_pass:
            return

        if self.board[move.coords[1]][move.coords[0]] != -1:
            raise IllegalMoveException("Space occupied")

        nb_chains = list({c for c in neighbours([move]) if c >= 0 and self.chains[c][0].player == move.player})
        if nb_chains:
            this_chain = nb_chains[0]
            self.board = [[nb_chains[0] if sq in nb_chains else sq for sq in line] for line in self.board]  # merge chains connected by this move
            for oc in nb_chains[1:]:
                self.chains[nb_chains[0]] += self.chains[oc]
                self.chains[oc] = []
            self.chains[nb_chains[0]].append(move)
        else:
            this_chain = len(self.chains)
            self.chains.append([move])
        self.board[move.coords[1]][move.coords[0]] = this_chain

        opp_nb_chains = {c for c in neighbours([move]) if c >= 0 and self.chains[c][0].player != move.player}
        for c in opp_nb_chains:
            if -1 not in neighbours(self.chains[c]):
                self.last_capture += self.chains[c]
                for om in self.chains[c]:
                    self.board[om.coords[1]][om.coords[0]] = -1
                self.chains[c] = []
        if ko_or_snapback and len(self.last_capture) == 1 and not ignore_ko:
            raise IllegalMoveException("Ko")
        self.prisoners += self.last_capture

        if -1 not in neighbours(self.chains[this_chain]): # TODO: NZ?
            raise IllegalMoveException("Suicide")

    # Play a Move from the current position, raise IllegalMoveException if invalid.
    def play(self, move: Move, ignore_ko: bool = False):
        if not move.is_pass and not (0 <= move.coords[0] < self.board_size and 0 <= move.coords[1] < self.board_size):
            raise IllegalMoveException(f"Move {move} outside of board coordinates")
        try:
            self._validate_move_and_update_chains(move, ignore_ko)
        except IllegalMoveException:
            raise
        played_node = self.current_node.play(move)
        self.current_node = played_node
        played_node.analyze(self.engine)
        return played_node

    def undo(self):
        if self.current_node is not self.root:
            self.current_node = self.current_node.parent
            self._init_chains()

    def redo(self):
        cn = self.current_node  # avoid race conditions
        if cn.children:
            self.current_node = cn.children[-1]
            self._init_chains()

    def switch_branch(self, direction):
        cm = self.current_node  # avoid race conditions
        if cm.parent and len(cm.parent.children) > 1:
            ix = cm.parent.children.index(cm)
            self.current_node = cm.parent.children[(ix + direction) % len(cm.parent.children)]
            self._init_chains()

    def place_handicap_stones(self, n_handicaps):
        near = 3 if self.board_size >= 13 else 2
        far = self.board_size - 1 - near
        middle = self.board_size // 2
        stones = [(far, far), (near, near), (far, near), (near, far)]
        if n_handicaps % 2 == 1:
            stones.append((middle, middle))
        stones += [(near, middle), (far, middle), (middle, near), (middle, far)]
        self.root.add_property("AB", [Move(stone).sgf(board_size=self.board_size) for stone in stones[:n_handicaps]])

    @property
    def next_player(self):
        return self.current_node.next_player

    @property
    def stones(self):
        return sum(self.chains, [])

    @property
    def game_ended(self):
        return self.current_node.parent and self.current_node.is_pass and self.current_node.parent.is_pass

    @property
    def prisoner_count(self):
        return [sum([m.player == player for m in self.prisoners]) for player in Move.PLAYERS]

    def __repr__(self):
        return "\n".join("".join(Move.PLAYERS[self.chains[c][0].player] if c >= 0 else "-" for c in line) for line in self.board) + f"\ncaptures: {self.prisoner_count}"

    def write_sgf(self, file_name=None):
        file_name = file_name or f"sgfout/katrain_{self.game_id}.sgf"
        os.makedirs(os.path.dirname(file_name), exist_ok=True)
        with open(file_name, "w") as f:
            f.write(self.root.sgf())
        return f"SGF with analysis written to {file_name}"

    def ai_move(self):
        if not self.current_node.analysis_ready:
            return  # TODO: hook/wait?

        # select move
        ai_moves = self.current_node.ai_moves
        pos_moves = [
            [d["move"], d["scoreLead"], d["pointsLost"]] for i, d in enumerate(ai_moves) if i == 0 or int(d["visits"]) >= self.config["balance_play_min_visits"]
        ]  # TODO: lcb based ?
        sel_moves = pos_moves[:1]
        # don't play suicidal to balance score - pass when it's best
        if self.katrain.controls.ai_balance.active and pos_moves[0][0] != "pass":  # TODO: settings where they belong?
            sel_moves = [
                (move, score, points_lost)
                for move, score, points_lost in pos_moves
                if points_lost < self.config["balance_play_randomize_eval"]
                or points_lost < self.config["balance_play_min_eval"]
                and -self.current_node.move.player_sign * score > self.config["balance_play_target_score"]
            ] or sel_moves
        aimove = Move.from_gtp(random.choice(sel_moves)[0], player=self.next_player)
        self.play(aimove)

    def num_undos(self, move):
        if self.config["num_undo_prompts"] < 1:
            return int(move.undo_threshold < self.config["num_undo_prompts"])
        else:
            return self.config["num_undo_prompts"]

    def analyze_extra(self, mode):
        stones = {s.coords for s in self.parent.game.stones}
        current_move = self.current_node
        if not current_move.analysis:
            self.info.text = "Wait for initial analysis to complete before doing a board-sweep or refinement"
            return
        played_moves = self.parent.game.moves

        if mode == "extra":
            visits = sum([d["visits"] for d in current_move.analysis]) + self.visits[0][1]
            self.info.text = f"Performing additional analysis to {visits} visits"
            self._request_analysis(current_move, min_visits=visits, priority=self.game_counter - 1_000)
            return
        elif mode == "sweep":
            analyze_moves = [Move(coords=(x, y)).gtp() for x in range(self.parent.game_size) for y in range(self.parent.game_size) if (x, y) not in stones]
            visits = self.visits[self.ai_fast.active][2]
            self.info.text = f"Refining analysis of entire board to {visits} visits"
            priority = self.game_counter - 1_000_000_000
        else:  # mode=='refine':
            analyze_moves = [a["move"] for a in current_move.analysis]
            visits = current_move.analysis[0]["visits"] + self.visits[1][2]
            self.info.text = f"Refining analysis of candidate moves to {visits} visits"
            priority = self.game_counter - 1_000

        for gtpcoords in analyze_moves:
            self._send_analysis_query(
                {
                    "id": f"AA:{current_move.id}:{gtpcoords}",
                    "moves": [[m.bw_player(), m.gtp()] for m in played_moves] + [[current_move.bw_player(True), gtpcoords]],
                    "includeOwnership": False,
                    "maxVisits": visits,
                    "priority": priority,
                }
            )
