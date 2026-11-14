"""bj v0.4 - twenty-one (blackjack) against Chiqu, for Butter. Points only: no money, nothing to lose.

Inside Butter (after  tul install bj  and  trust add bj.py):
    py bj.py                 show the table (or how to start)
    py bj.py deal [BET]      new round; the bet is 10 points if you do not say      (also: py bj.py 25)
    py bj.py hit             take a card                                            (short: h)
    py bj.py stand           stop here, Chiqu plays the hand                         (short: s)
    py bj.py double          double the bet, take exactly one card, then stand      (short: dd; first move only)
    py bj.py points          your points and your results                           (short: p)
    py bj.py reset --yes     start again from 100 points

Rules: one fresh deck every round. Get 21, or as close as you can without going over. Cards 2-10 count as written,
J Q K count 10, an Ace counts 11 or 1. Chiqu takes cards until the total is 17 and stops on any 17. Two cards that make
21 (a blackjack) pay 3 to 2. A tie gives your bet back. There is no split and no insurance. If your points run out,
Chiqu lends you 50. The game is saved in bj_save.json next to bj.py.

Cards are written like [AS] [10H] [KD] [7C]: rank, then suit (S spades, H hearts, D diamonds, C clubs).
"""
import json
import os
import random
import re
import sys

VERSION = "0.4"
NAME = "Chiqu"
CMD = "py bj.py"                 # how the game is started; change it to "bj" if Butter gets a bj command
START_POINTS = 100
LOAN = 50
DEFAULT_BET = 10
MAX_BET = 1000000

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = "SHDC"
CARD_RE = re.compile(r"^(A|10|[2-9]|J|Q|K)[SHDC]$")
INT_KEYS = ("points", "best", "rounds", "wins", "losses", "pushes", "blackjacks", "loans")


CLEAR_MODE = os.environ.get("BJ_CLEAR", "ansi")   # "ansi" wipes the screen, "lines" scrolls it away with empty lines, "off" does nothing


def say(text=""):
    print(text)


def clear_screen():
    """The table is a 'window': every move wipes the old text, so Chiqu always stays at the top."""
    if CLEAR_MODE == "ansi":
        sys.stdout.write("\033[3J\033[2J\033[H")
        sys.stdout.flush()
    elif CLEAR_MODE == "lines":
        say("\n" * 60)


class GameError(Exception):          # wrong use of the command: [ERROR], exit code 1
    pass


class Notice(Exception):             # a friendly "not now": [INFO], no error
    pass


# ---------------------------------------------------------------- Chiqu
# The drawing: L and R are the eyes, M is the mouth (they change with the mood). Every row is raw text, so backslashes are fine.
CAT = r"""
   /\_/\
  ( L.R )
   > M <
  /  |  \
 (_/   \_)
""".strip("\n").split("\n")
CAT_INDENT = 4                   # spaces in front of every row of the drawing
EYES = {"idle": ("o", "o"), "happy": ("^", "^"), "wow": ("O", "O"),
        "sad": ("T", "T"), "wink": ("^", "-"), "smug": ("-", "-")}
MOUTH = {"idle": "^", "happy": "w", "wow": "o", "sad": "n", "wink": "^", "smug": "v"}


def cat(mood):
    left, right = EYES[mood]
    return [" " * CAT_INDENT + row.replace("L", left).replace("R", right).replace("M", MOUTH[mood]) for row in CAT]


LINES = {
    "welcome": ["Meow! I'm %s. Want to play 21?" % NAME, "Purr... sit down. A round of 21?"],
    "deal": ["Cards are out. Meow!", "Let's play! Hit or stand?", "Ready? Your move."],
    "hit": ["One more card? Brave!", "Here you go. Meow.", "Hmm, that's risky..."],
    "turn": ["Your move!", "Hit or stand?"],
    "double": ["Double it?! Bold!", "Ooh, all in on one card!"],
    "win": ["Meow?! You beat me. Fair and square.", "Nice hand! I'll get you next time.",
            "Okay, okay, that one is yours.", "Tch... beginner's luck?"],
    "blackjack": ["Blackjack?! No way... 3 to 2 for you.", "A natural! I'm impressed!"],
    "lose": ["Mine! Better luck next time~", "I win this one. Cards don't lie.", "Purr... the house always smiles."],
    "bust": ["Over 21! Careful, careful~", "Oops, too greedy!", "Bust! What a pity."],
    "push": ["A tie! Your bet is safe.", "Same score. Let's call it even."],
    "loan": ["Out of points? I'll lend you %d from my fish money. Shh!" % LOAN],
}
OUTCOMES = {"win": "wow", "blackjack": "wow", "lose": "smug", "bust": "smug", "push": "wink"}


def pick(kind):
    return random.choice(LINES[kind])


# ---------------------------------------------------------------- cards
def new_deck():
    rng = random.Random(os.environ.get("BJ_SEED") or None)       # BJ_SEED is only for tests
    deck = [r + s for r in RANKS for s in SUITS]
    rng.shuffle(deck)
    return deck


def value(hand):
    """(best total, is it soft?) - an Ace counts 11 until that would be over 21."""
    total, aces = 0, 0
    for card in hand:
        rank = card[:-1]
        if rank == "A":
            total += 11
            aces += 1
        elif rank in ("J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, aces > 0


def natural(hand):
    return len(hand) == 2 and value(hand)[0] == 21


def show_cards(hand, hide_second=False):
    shown = ["[%s]" % c for c in hand]
    if hide_second and len(shown) > 1:
        shown = shown[:1] + ["[??]"]
    return " ".join(shown)


# ---------------------------------------------------------------- the save file
def save_path():
    base = os.environ.get("BJ_HOME")
    if not base:
        try:
            base = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            base = os.getcwd()
    return os.path.join(base, "bj_save.json")


def new_state():
    state = {k: 0 for k in INT_KEYS}
    state["points"] = state["best"] = START_POINTS
    state["round"] = None
    return state


def good_int(v, low=0, high=None):
    return isinstance(v, int) and not isinstance(v, bool) and v >= low and (high is None or v <= high)


def good_cards(cards, low, high):
    return isinstance(cards, list) and low <= len(cards) <= high and all(isinstance(c, str) and CARD_RE.match(c) for c in cards)


def load():
    """(state, was the file damaged?). A file with wrong content is never trusted."""
    try:
        with open(save_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return new_state(), False
    except (OSError, ValueError):
        return new_state(), True
    if not isinstance(data, dict):
        return new_state(), True
    state = new_state()
    for key in INT_KEYS:
        if good_int(data.get(key)):
            state[key] = data[key]
    rnd = data.get("round")
    if isinstance(rnd, dict) and good_int(rnd.get("bet"), 1, MAX_BET) and good_cards(rnd.get("player"), 2, 12) \
            and good_cards(rnd.get("dealer"), 2, 12) and good_cards(rnd.get("deck"), 0, 52):
        state["round"] = {"bet": rnd["bet"], "deck": rnd["deck"], "player": rnd["player"],
                          "dealer": rnd["dealer"], "doubled": bool(rnd.get("doubled"))}
    return state, False


def save(state):
    path = save_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError as e:
        raise GameError("The game could not be saved: %s" % (e.strerror or e))


# ---------------------------------------------------------------- drawing the table
def render(state, mood, line, player, dealer, bet, hide):
    clear_screen()
    for row in cat(mood):
        say(row)
    say('%s: "%s"' % (NAME, line))
    if hide:
        say("Dealer: %s" % show_cards(dealer, True))
    else:
        say("Dealer: %s  = %d" % (show_cards(dealer), value(dealer)[0]))
    total, soft = value(player)
    say("You   : %s  = %d%s" % (show_cards(player), total, " (soft)" if soft and total < 21 else ""))
    say("Bet %d | Points %d" % (bet, state["points"]))


def show_round(state, mood, line):
    rnd = state["round"]
    render(state, mood, line, rnd["player"], rnd["dealer"], rnd["bet"], True)
    options = "hit | stand"
    if len(rnd["player"]) == 2 and not rnd["doubled"] and state["points"] >= rnd["bet"]:
        options += " | double"
    say("[INFO] Next: %s %s" % (CMD, options))


# ---------------------------------------------------------------- one round
def settle(state, rnd, outcome):
    """Pay out (the bet was already taken at the deal). Returns True if Chiqu had to lend points."""
    bet = rnd["bet"]
    state["rounds"] += 1
    if outcome == "blackjack":
        state["points"] += bet + (bet * 3) // 2
        state["wins"] += 1
        state["blackjacks"] += 1
    elif outcome == "win":
        state["points"] += 2 * bet
        state["wins"] += 1
    elif outcome == "push":
        state["points"] += bet
        state["pushes"] += 1
    else:                                                   # lose or bust
        state["losses"] += 1
    state["best"] = max(state["best"], state["points"])
    state["round"] = None
    if state["points"] <= 0:
        state["points"] = LOAN
        state["loans"] += 1
        return True
    return False


def end_round(state, rnd, outcome):
    bet, player, dealer = rnd["bet"], list(rnd["player"]), list(rnd["dealer"])
    loaned = settle(state, rnd, outcome)
    save(state)
    render(state, OUTCOMES[outcome], pick(outcome), player, dealer, bet, False)
    if outcome == "blackjack":
        say("[SUCCESS] Blackjack! You win %d points." % ((bet * 3) // 2))
    elif outcome == "win":
        say("[SUCCESS] You win %d points." % bet)
    elif outcome == "push":
        say("[INFO] A tie: your %d points come back." % bet)
    else:
        say("[WARNING] You lose %d points." % bet)
    if loaned:
        say('%s: "%s"' % (NAME, pick("loan")))
        say("[INFO] You have %d points again." % state["points"])
    else:
        say("[INFO] New round: %s deal [bet]" % CMD)


def stand_now(state, rnd):
    while value(rnd["dealer"])[0] < 17:
        rnd["dealer"].append(rnd["deck"].pop())
    mine, hers = value(rnd["player"])[0], value(rnd["dealer"])[0]
    if hers > 21 or mine > hers:
        end_round(state, rnd, "win")
    elif mine == hers:
        end_round(state, rnd, "push")
    else:
        end_round(state, rnd, "lose")


def need_round(state):
    if not state["round"]:
        raise Notice("No round is on. Start one: %s deal [bet]" % CMD)
    return state["round"]


# ---------------------------------------------------------------- commands
def parse_bet(text, points):
    if not text.isdigit():
        raise GameError("The bet is a whole number of points, for example: %s deal 25" % CMD)
    bet = int(text)
    if bet < 1:
        raise GameError("The smallest bet is 1 point.")
    if bet > MAX_BET or bet > points:
        raise GameError("You have only %d point(s)." % points)
    return bet


def cmd_deal(state, args):
    if state["round"]:
        raise Notice("A round is already on. Take a card (hit) or stop (stand). Table: %s" % CMD)
    if len(args) > 1:
        raise GameError("Usage: %s deal [BET]" % CMD)
    bet = parse_bet(args[0], state["points"]) if args else min(DEFAULT_BET, state["points"])
    state["points"] -= bet
    deck = new_deck()
    player, dealer = [deck.pop()], [deck.pop()]
    player.append(deck.pop())
    dealer.append(deck.pop())
    state["round"] = rnd = {"bet": bet, "deck": deck, "player": player, "dealer": dealer, "doubled": False}
    if natural(player) and natural(dealer):
        end_round(state, rnd, "push")
    elif natural(player):
        end_round(state, rnd, "blackjack")
    elif natural(dealer):
        end_round(state, rnd, "lose")
    else:
        save(state)
        show_round(state, "idle", pick("deal"))


def cmd_hit(state, args):
    rnd = need_round(state)
    rnd["player"].append(rnd["deck"].pop())
    total = value(rnd["player"])[0]
    if total > 21:
        end_round(state, rnd, "bust")
    elif total == 21:
        stand_now(state, rnd)                                # 21 cannot get better: Chiqu plays at once
    else:
        save(state)
        show_round(state, "idle", pick("hit"))


def cmd_stand(state, args):
    stand_now(state, need_round(state))


def cmd_double(state, args):
    rnd = need_round(state)
    if len(rnd["player"]) != 2 or rnd["doubled"]:
        raise Notice("You can double only as your first move.")
    if state["points"] < rnd["bet"]:
        raise Notice("Not enough points to double (have %d)." % state["points"])
    state["points"] -= rnd["bet"]
    rnd["bet"] *= 2
    rnd["doubled"] = True
    rnd["player"].append(rnd["deck"].pop())
    if value(rnd["player"])[0] > 21:
        end_round(state, rnd, "bust")
    else:
        stand_now(state, rnd)


def cmd_table(state, args):
    if state["round"]:
        show_round(state, "idle", pick("turn"))
        return
    clear_screen()
    for row in cat("happy"):
        say(row)
    say('%s: "%s"' % (NAME, pick("welcome")))
    say("[INFO] You have %d points. Start: %s deal [bet]" % (state["points"], CMD))


def cmd_points(state, args):
    say("Points: %d (best %d)" % (state["points"], state["best"]))
    say("Rounds: %d - won %d (blackjacks %d), lost %d, tied %d" % (
        state["rounds"], state["wins"], state["blackjacks"], state["losses"], state["pushes"]))
    if state["loans"]:
        say("%s lent you points %d time(s)." % (NAME, state["loans"]))


def cmd_reset(state, args):
    if args != ["--yes"]:
        raise Notice("This erases your points and results. If you are sure: %s reset --yes" % CMD)
    save(new_state())
    say("[SUCCESS] Fresh start: %d points." % START_POINTS)


def cmd_help(state, args):
    say("bj %s - 21 against %s. Points only." % (VERSION, NAME))
    say("  %s deal [BET]   new round (default bet %d)" % (CMD, DEFAULT_BET))
    say("  %s hit | stand | double" % CMD)
    say("  %s points | reset --yes" % CMD)
    say("Goal: 21 or as close as you can, not over. Blackjack pays 3:2.")


COMMANDS = {"table": cmd_table, "t": cmd_table, "deal": cmd_deal, "hit": cmd_hit, "h": cmd_hit,
            "stand": cmd_stand, "s": cmd_stand, "double": cmd_double, "dd": cmd_double,
            "points": cmd_points, "p": cmd_points, "reset": cmd_reset}


def main(argv):
    args = argv[1:]
    command = args[0].lower() if args else "table"
    rest = args[1:]
    if command in ("help", "-h", "--help", "?"):
        cmd_help(None, [])
        return 0
    if command.isdigit():                                    # "bj.py 25" = "bj.py deal 25"
        command, rest = "deal", [command]
    handler = COMMANDS.get(command)
    if handler is None:
        say("[ERROR] Unknown command '%s'. Try: %s help" % (command, CMD))
        return 1
    damaged = False
    try:
        state, damaged = load()
        handler(state, rest)
    except Notice as e:
        say("[INFO] %s" % e)
    except GameError as e:
        say("[ERROR] %s" % e)
        return 1
    finally:
        if damaged:
            say("[WARNING] The save file was damaged, so we start again with %d points." % START_POINTS)
    return 0


if __name__ == "__main__":
    try:
        rc = main(sys.argv)
    except KeyboardInterrupt:
        say("[ERROR] stopped")
        rc = 1
    try:
        sys.stdout.flush()
    except Exception:
        pass
    if rc:                                                   # exit only on failure (like the other tools)
        sys.exit(rc)
