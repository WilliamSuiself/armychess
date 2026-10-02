"""Core Tetris engine: pieces, rotation, collision, line clear, garbage.

Board is a list of ROWS lists of COLS cells; each cell is either None
(empty) or a single-letter piece tag ('I','O','T','S','Z','J','L') or
'X' for a garbage block. No external dependencies — pure game rules,
independent of Flask / AI backends so it can be unit-tested on its own.
"""

import random

ROWS, COLS = 20, 10

# Base shape (row, col) offsets inside an NxN bounding box, rotation 0.
BASE_SHAPES = {
    "I": (4, {(1, 0), (1, 1), (1, 2), (1, 3)}),
    "O": (2, {(0, 0), (0, 1), (1, 0), (1, 1)}),
    "T": (3, {(0, 1), (1, 0), (1, 1), (1, 2)}),
    "S": (3, {(0, 1), (0, 2), (1, 0), (1, 1)}),
    "Z": (3, {(0, 0), (0, 1), (1, 1), (1, 2)}),
    "J": (3, {(0, 0), (1, 0), (1, 1), (1, 2)}),
    "L": (3, {(0, 2), (1, 0), (1, 1), (1, 2)}),
}

COLORS = {
    "I": "#22d3ee", "O": "#facc15", "T": "#c084fc", "S": "#4ade80",
    "Z": "#f87171", "J": "#60a5fa", "L": "#fb923c", "X": "#64748b",
}


def _rotate_cw(cells, n):
    return {(c, n - 1 - r) for (r, c) in cells}


def _build_rotations():
    out = {}
    for letter, (n, cells) in BASE_SHAPES.items():
        states = [cells]
        cur = cells
        for _ in range(3):
            cur = _rotate_cw(cur, n)
            states.append(cur)
        out[letter] = [sorted(s) for s in states]
    return out


ROTATIONS = _build_rotations()  # letter -> [state0, state1, state2, state3]


def new_bag(rng=None):
    bag = list(BASE_SHAPES.keys())
    (rng or random).shuffle(bag)
    return bag


class Piece:
    __slots__ = ("letter", "rot", "row", "col")

    def __init__(self, letter, rot=0, row=0, col=None):
        self.letter = letter
        self.rot = rot
        self.row = row
        self.col = col if col is not None else (COLS - _bbox_size(letter)) // 2

    def cells(self, rot=None, row=None, col=None):
        rot = self.rot if rot is None else rot
        row = self.row if row is None else row
        col = self.col if col is None else col
        return [(row + r, col + c) for (r, c) in ROTATIONS[self.letter][rot % 4]]

    def clone(self):
        return Piece(self.letter, self.rot, self.row, self.col)


def _bbox_size(letter):
    return BASE_SHAPES[letter][0]


def new_board():
    return [[None] * COLS for _ in range(ROWS)]


def spawn_piece(letter):
    return Piece(letter, rot=0, row=0, col=(COLS - _bbox_size(letter)) // 2)


def cells_valid(board, cells):
    for (r, c) in cells:
        if c < 0 or c >= COLS or r >= ROWS:
            return False
        if r >= 0 and board[r][c] is not None:
            return False
    return True


def try_move(board, piece, dr, dc):
    cells = piece.cells(row=piece.row + dr, col=piece.col + dc)
    if cells_valid(board, cells):
        return Piece(piece.letter, piece.rot, piece.row + dr, piece.col + dc)
    return None


# Simple (non-SRS) wall-kick offsets tried in order when a plain rotation
# would collide — good enough for a casual game, not guideline-accurate.
_KICKS = [(0, 0), (0, -1), (0, 1), (0, -2), (0, 2), (-1, 0), (1, 0)]


def try_rotate(board, piece, direction=1):
    new_rot = (piece.rot + direction) % 4
    for dr, dc in _KICKS:
        row, col = piece.row + dr, piece.col + dc
        cells = piece.cells(rot=new_rot, row=row, col=col)
        if cells_valid(board, cells):
            return Piece(piece.letter, new_rot, row, col)
    return None


def hard_drop_row(board, piece):
    """Return the piece landed as far down as it can go."""
    cur = piece
    while True:
        nxt = try_move(board, cur, 1, 0)
        if nxt is None:
            return cur
        cur = nxt


def lock_piece(board, piece):
    """Write piece cells into board. Returns True if any cell landed
    above the visible field (a hard top-out)."""
    overflow = False
    for (r, c) in piece.cells():
        if r < 0:
            overflow = True
            continue
        board[r][c] = piece.letter
    return overflow


def clear_full_lines(board):
    full_idx = [r for r in range(ROWS) if all(cell is not None for cell in board[r])]
    if not full_idx:
        return 0
    keep = [row for r, row in enumerate(board) if r not in full_idx]
    n = len(full_idx)
    new_rows = [[None] * COLS for _ in range(n)]
    board[:] = new_rows + keep
    return n


GARBAGE_TABLE = {0: 0, 1: 0, 2: 1, 3: 2, 4: 4}


def garbage_for_clear(lines_cleared, combo=0):
    base = GARBAGE_TABLE.get(lines_cleared, 0)
    if combo >= 2:
        base += min(combo - 1, 4)  # small combo bonus, capped
    return base


def add_garbage(board, n, hole_col=None):
    """Push `n` garbage rows in from the bottom, shifting the stack up.
    Returns True if this caused blocks to be pushed off the top (top-out)."""
    if n <= 0:
        return False
    if hole_col is None:
        hole_col = random.randrange(COLS)
    # Rows that get shifted past the top are lost — if any held blocks,
    # that's a top-out for this side.
    overflow = any(
        any(cell is not None for cell in board[r]) for r in range(min(n, ROWS))
    )
    del board[0:n]
    for _ in range(n):
        row = ["X"] * COLS
        row[hole_col] = None
        board.append(row)
    return overflow


def column_heights(board):
    heights = [0] * COLS
    for c in range(COLS):
        for r in range(ROWS):
            if board[r][c] is not None:
                heights[c] = ROWS - r
                break
    return heights


def count_holes(board):
    holes = 0
    for c in range(COLS):
        seen_block = False
        for r in range(ROWS):
            if board[r][c] is not None:
                seen_block = True
            elif seen_block:
                holes += 1
    return holes


def bumpiness(heights):
    return sum(abs(heights[i] - heights[i + 1]) for i in range(len(heights) - 1))


def board_to_rows(board):
    """Compact text rows ('.' empty, letter filled) — top row first."""
    return ["".join(cell if cell else "." for cell in row) for row in board]
