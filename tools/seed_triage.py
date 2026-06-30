"""seed_triage — Discord-reaction → durable triage state adapter (v0.1).

The first consumer of the durable-state core `tools/reaction_state.py`. It turns
Ace's ✅/👍/❌ reactions on greenhouse seed cards into durable triage state by:

  1. building a target message set (every message with a durable present key in the
     DB  ∪  every seed-card message in seed maps newer than --window-days),
  2. polling each message's reactions over Discord REST (paginated, emoji-encoded),
  3. filtering to Ace's user-id (the bot's own pre-seeds and other members are NOT
     triage signal),
  4. building the core's WATERMARKED snapshot and folding it through the UNMODIFIED
     `reaction_state.reconcile()`,
  5. printing a durable per-seed verdict (`✅ build · 👍 interesting · ❌ not for me`).

It is a stateless REST poller, NOT a gateway listener: the greenhouse bot is already
on the gateway and Discord permits one session per token, so a second websocket would
fight the live bot. No `discord.py`, no journal, no write-back into greenhouse.

Spec: docs/seed_triage_adapter_prd.md (APPROVED v0.3, folded Opus pass-1 + pass-2).

Hard contracts (see the PRD invariants):
  * INV-1  consumes `reaction_state` UNMODIFIED (reconcile / current_present / connect).
  * INV-2  triage signal = a reaction by Ace's user-id ONLY.
  * INV-3  removes require PROVEN-COMPLETE global coverage (no fetch-fail delete).
  * D-3 ⚠️ watermark = COALESCE(MAX(seq),0)+1 over ALL rows (NOT WHERE present=1 —
           that is the Variant-B silent re-react data-loss bug).
  * INV-9  single-writer via fcntl.flock (auto-releases on process death).
  * D-11   NO fabricated "since": the core's reconcile path has no real timestamp.
  * D-14   this --check-target is REST-liveness, NOT the core's journal probe.

Stdlib only (urllib/fcntl are stdlib; the *core* stays import-restricted, this adapter
does not).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Import the durable-state core UNMODIFIED (INV-1). conftest.py puts the repo root
# on sys.path so `from tools import ...` works under pytest and direct invocation.
try:
    from tools import reaction_state as rs
except ImportError:  # direct `python tools/seed_triage.py` from repo root
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools import reaction_state as rs


API = "https://discord.com/api/v10"

# The triage emoji and their verdict labels. ❌ > ✅ > 👍 (D-7): a "no" is the
# strongest signal; an explicit build beats a soft "interesting".
EMOJI_DEVELOP = "✅"
EMOJI_INTERESTING = "👍"
EMOJI_NOISE = "❌"
TRIAGE_EMOJI = (EMOJI_DEVELOP, EMOJI_INTERESTING, EMOJI_NOISE)
_PRECEDENCE = {EMOJI_NOISE: 3, EMOJI_DEVELOP: 2, EMOJI_INTERESTING: 1}
_VERDICT_LABEL = {
    EMOJI_NOISE: "❌ not for me",
    EMOJI_DEVELOP: "✅ build it",
    EMOJI_INTERESTING: "👍 interesting",
}

DEFAULT_ACE_USER_ID = "117431298246705156"
DEFAULT_SEEDS_DIR = str(Path.home() / ".hermes" / "greenhouse" / "seeds")
DEFAULT_WINDOW_DAYS = 14


class APIError(Exception):
    """A real, loud Discord REST failure — never swallowed into a silent green."""


# --- REST transport (injectable for tests) ----------------------------------
def _default_opener(req: urllib.request.Request, timeout: int = 20):
    # Hard-gate the scheme to https before opening: every request this adapter
    # builds targets the constant Discord REST host, so a file:// or other custom
    # scheme can only mean a malformed/poisoned URL. Refuse it loudly instead of
    # letting urlopen honor an unexpected scheme (CWE-22).
    if req.type != "https":
        raise APIError(f"refusing non-https request scheme: {req.type!r}")
    return urllib.request.urlopen(req, timeout=timeout)  # nosec B310 - scheme gated to https above


def _token() -> str:
    tok = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not tok:
        raise APIError("DISCORD_BOT_TOKEN not set")
    return tok


def api_get(path: str, params: dict | None = None, *, token: str | None = None,
            opener=_default_opener, _retries: int = 4):
    """GET the Discord API. Honors 429 Retry-After, retries transient 5xx, and
    raises APIError (loud) on a hard failure. The token is read from env unless
    injected; it is NEVER logged or embedded in an exception message (INV-6)."""
    import time
    tok = token if token is not None else _token()
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last: Exception | None = None
    for attempt in range(_retries):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bot {tok}",
            "User-Agent": "DiscordBot (greenhouse-seed-triage, 1.0)",
        })
        try:
            with opener(req) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # Never let an HTTPError repr (which can carry the request/headers) leak
            # the token: raise a clean APIError naming only the code + path.
            if e.code == 429:
                retry = 1.0
                try:
                    retry = float(e.headers.get("retry-after", "1")) + 0.5
                except (TypeError, ValueError):
                    retry = 1.5
                time.sleep(min(retry, 10))
                last = e
                continue
            if e.code in (500, 502, 503):
                time.sleep(1 + attempt)
                last = e
                continue
            raise APIError(f"HTTP {e.code} on {path}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            time.sleep(1 + attempt)
            last = e
    raise APIError(f"exhausted retries on {path}") from None


def reaction_users(channel_id: str, message_id: str, emoji: str, *,
                   token: str | None = None, opener=_default_opener) -> list[str]:
    """All user-ids who reacted with `emoji` on a message, fully paginated and
    emoji-path-encoded so >100 reactors can't hide Ace (D-8)."""
    enc = urllib.parse.quote(emoji)
    users: list[str] = []
    after = None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        page = api_get(
            f"/channels/{channel_id}/messages/{message_id}/reactions/{enc}",
            params, token=token, opener=opener,
        )
        if not page:
            break
        users += [str(u["id"]) for u in page]
        if len(page) < 100:
            break
        after = page[-1]["id"]
    return users


# --- seed maps (schema-guarded) ---------------------------------------------
def load_seed_maps(seeds_dir: str | Path) -> dict:
    """Load EVERY seed_messages.json under seeds_dir. Returns
    {message_id: {"channel_id","seed_id","run_dir","mtime"}}. A malformed/renamed
    map is skipped with a LOUD warning, never a silent empty result (INV-8)."""
    out: dict[str, dict] = {}
    base = Path(seeds_dir)
    if not base.exists():
        return out
    for run_dir in sorted(base.iterdir()):
        if not run_dir.is_dir():
            continue
        sm_path = run_dir / "seed_messages.json"
        if not sm_path.exists():
            continue
        try:
            doc = json.loads(sm_path.read_text(encoding="utf-8"))
            seed_messages = doc["seed_messages"]
            if not isinstance(seed_messages, dict):
                raise ValueError("seed_messages is not an object")
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"[seed_triage] WARNING: skipping malformed seed map {sm_path}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        try:
            mtime = sm_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        for seed_id, sm in seed_messages.items():
            try:
                mid = str(sm["message_id"])
                ch = str(sm["channel_id"])
            except (TypeError, KeyError):
                print(f"[seed_triage] WARNING: seed {seed_id} in {sm_path} missing "
                      f"channel_id/message_id; skipped", file=sys.stderr)
                continue
            out[mid] = {"channel_id": ch, "seed_id": seed_id,
                        "run_dir": str(run_dir), "mtime": mtime}
    return out


def load_seed_titles(seeds_dir: str | Path) -> dict:
    """{seed_id: title} from every run.json under seeds_dir (best-effort)."""
    titles: dict[str, str] = {}
    base = Path(seeds_dir)
    if not base.exists():
        return titles
    for run_dir in sorted(base.iterdir()):
        rj = run_dir / "run.json"
        if not rj.exists():
            continue
        try:
            doc = json.loads(rj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for s in (doc.get("seeds") or doc.get("planted") or []):
            sid = s.get("seed_id")
            if sid:
                titles[sid] = s.get("title", "")
    return titles


# --- targets + watermark (D-3 ⚠️, D-4) --------------------------------------
def db_present_messages(conn) -> set[tuple[str, str]]:
    """Distinct (channel_id, message_id) for every durable PRESENT key (D-4 half a)."""
    return {(ck, mk) for (ck, mk, _e, _u) in rs.current_present(conn)}


def next_watermark(conn) -> int:
    """COALESCE(MAX(seq),0)+1 over the FULL reaction_state table — NO present=1
    filter (D-3 ⚠️). Filtering on present=1 is the Variant-B silent re-react bug:
    a removed row at the current max seq would be invisible, so a re-react's
    synthesized ADD would land <= the prior REMOVE's seq and the core would reject
    it as stale, silently losing Ace's verdict. Empirically verified."""
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM reaction_state").fetchone()
    return int(row[0]) + 1


def build_targets(conn, seed_maps: dict, window_days: int) -> list[tuple[str, str]]:
    """Target message set = (every durable-present-key message) ∪ (seed-card
    messages in seed maps newer than window_days). The DB-union half is MANDATORY
    for INV-3 completeness; the window half bounds new-card cost (D-4)."""
    import time
    targets: set[tuple[str, str]] = set(db_present_messages(conn))
    cutoff = time.time() - window_days * 86400
    for mid, m in seed_maps.items():
        if m["mtime"] >= cutoff:
            targets.add((m["channel_id"], mid))
    return sorted(targets)


# --- snapshot build (INV-2, INV-3) ------------------------------------------
def build_snapshot(targets, ace_user_id, watermark, *, token=None,
                   opener=_default_opener):
    """Fetch all 3 triage emoji per target message, keep ONLY Ace's reactions
    (INV-2), and track per-message fetch success so the caller can decide
    `covers_removes` (INV-3). Returns (snapshot, fetch_ok: {(ch,mid): bool})."""
    reactions = []
    fetch_ok: dict[tuple[str, str], bool] = {}
    for ch, mid in targets:
        ok = True
        for emoji in TRIAGE_EMOJI:
            try:
                users = reaction_users(ch, mid, emoji, token=token, opener=opener)
            except APIError as exc:
                # A failed fetch means this message is NOT fully enumerated; mark it
                # incomplete so it can never drive a remove-on-absence (INV-3).
                print(f"[seed_triage] fetch_fail {ch}/{mid} {emoji}: {exc}",
                      file=sys.stderr)
                ok = False
                continue
            if str(ace_user_id) in users:
                reactions.append({
                    "channel_id": ch, "message_id": mid,
                    "emoji": emoji, "user_id": str(ace_user_id),
                })
        fetch_ok[(ch, mid)] = ok
    snapshot = {"watermark": watermark, "covers_removes": False,
                "reactions": reactions}
    return snapshot, fetch_ok


def compute_covers_removes(conn, targets, fetch_ok) -> tuple[bool, list]:
    """`covers_removes` is True IFF this poll PROVEN-COMPLETELY enumerated every
    durable present key (INV-3): (a) the target set ⊇ every durable-present-key
    message, AND (b) every per-message fetch succeeded. Returns (eligible,
    uncovered_keys) where uncovered_keys names the durable messages this poll did
    NOT fully cover (for the D-13 chronic-disable alert)."""
    target_set = set(targets)
    durable = db_present_messages(conn)
    uncovered = []
    for key in sorted(durable):
        if key not in target_set or not fetch_ok.get(key, False):
            uncovered.append(key)
    return (len(uncovered) == 0), uncovered


# --- the lock (INV-9: fcntl.flock, auto-releases on death) -------------------
class _FlockBusy(Exception):
    pass


class poll_lock:
    """Exclusive advisory lock via fcntl.flock — auto-released when the fd closes
    or the process dies (INV-9). NOT a presence file: a crashed poll never wedges
    future polls. Raises _FlockBusy if another poll holds it."""

    def __init__(self, db_path: str | Path):
        self._path = str(db_path) + ".lock"
        self._fd = None

    def __enter__(self):
        self._fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            os.close(self._fd)
            self._fd = None
            raise _FlockBusy(f"another poll holds {self._path}")
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        return False


# --- poll (the safe reconcile fold) -----------------------------------------
def poll(db_path, seed_maps, ace_user_id, window_days, *, token=None,
         opener=_default_opener, narrow_targets=None):
    """One poll pass: build targets, fetch+filter to Ace, compute a SAFE
    covers_removes, and fold through the UNMODIFIED core reconcile under the flock.
    Returns a stats dict. `narrow_targets` (e.g. an ad-hoc --message) forces
    add-only because it cannot cover all durable keys (INV-3/D-6)."""
    conn = rs.connect(str(db_path))
    try:
        with poll_lock(db_path):
            wm = next_watermark(conn)
            if narrow_targets is not None:
                targets = sorted(set(narrow_targets))
                is_narrow = True
            else:
                targets = build_targets(conn, seed_maps, window_days)
                is_narrow = False
            snapshot, fetch_ok = build_snapshot(
                targets, ace_user_id, wm, token=token, opener=opener)
            covers, uncovered = compute_covers_removes(conn, targets, fetch_ok)
            # A narrow poll can never claim complete coverage → add-only (D-6).
            eligible = covers and not is_narrow
            snapshot["covers_removes"] = eligible
            result = rs.reconcile(conn, snapshot, allow_removes=eligible)
            fetch_fail = sum(1 for v in fetch_ok.values() if not v)
            stats = {
                "targets": len(targets),
                "polled": len(fetch_ok),
                "fetch_fail": fetch_fail,
                "ace_reactions": len(snapshot["reactions"]),
                "added": result["added"],
                "removed": result["removed"],
                "covers_removes": eligible,
                "uncovered_keys": uncovered,
                "narrow": is_narrow,
            }
            return stats
    finally:
        conn.close()


# --- report (read-model; NO fabricated "since" — D-11) ----------------------
def _verdict_for_message(conn, channel_id, message_id, ace_user_id):
    """The single highest-precedence emoji Ace has DURABLY present on a message,
    or None. Precedence ❌>✅>👍 (D-7) matters only when >1 is present at once."""
    present = rs.current_present(conn)
    emojis = [e for (ck, mk, e, u) in present
              if ck == channel_id and mk == message_id and u == str(ace_user_id)
              and e in _PRECEDENCE]
    if not emojis:
        return None
    return max(emojis, key=lambda e: _PRECEDENCE[e])


def report(conn, seed_maps, seed_titles, ace_user_id, window_days, *,
           as_json=False):
    """Join durable present keys ⨯ ALL seed maps ⨯ precedence → per-seed verdict.
    Row = seed_id · title · verdict (NO "since" — the core's reconcile path has no
    real timestamp, D-11). Also surfaces un-triaged cards aged past the capture
    horizon (D-12). Returns a dict (and renders text when not as_json)."""
    import time
    rows = []
    for mid, m in sorted(seed_maps.items(), key=lambda kv: kv[1]["seed_id"]):
        v = _verdict_for_message(conn, m["channel_id"], mid, ace_user_id)
        if v is None:
            continue
        rows.append({"seed_id": m["seed_id"],
                     "title": seed_titles.get(m["seed_id"], ""),
                     "verdict": _VERDICT_LABEL[v], "emoji": v})

    # Durable present keys whose message is in NO seed map → unknown-seed (D-9),
    # never dropped.
    mapped_msgs = set(seed_maps.keys())
    for (ck, mk, e, u) in sorted(rs.current_present(conn)):
        if u != str(ace_user_id) or e not in _PRECEDENCE:
            continue
        if mk in mapped_msgs:
            continue
        rows.append({"seed_id": "unknown-seed", "title": f"(message {mk})",
                     "verdict": _VERDICT_LABEL[e], "emoji": e})

    # Aged-out un-triaged cards: a seed map older than the window with no durable
    # verdict (D-12) — surfaced, never a silent miss.
    cutoff = time.time() - window_days * 86400
    triaged_seed_ids = {r["seed_id"] for r in rows}
    aged_out = []
    for mid, m in seed_maps.items():
        if m["mtime"] < cutoff and m["seed_id"] not in triaged_seed_ids:
            aged_out.append(m["seed_id"])

    result = {
        "ace_user_id": str(ace_user_id),
        "rows": sorted(rows, key=lambda r: r["seed_id"]),
        "aged_out_untriaged": sorted(set(aged_out)),
    }
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    print(f"seed_triage report (triage signal = user {ace_user_id})")
    if not result["rows"]:
        print("  (no durable verdicts yet)")
    for r in result["rows"]:
        title = (r["title"][:60]) if r["title"] else ""
        print(f"  {r['seed_id']:<22} {r['verdict']:<16} {title}")
    if result["aged_out_untriaged"]:
        n = len(result["aged_out_untriaged"])
        print(f"  ⚠️ {n} un-triaged card(s) aged out of the {window_days}-day "
              f"capture horizon: {', '.join(result['aged_out_untriaged'])}")
    return result


# --- --selfcheck (offline logic probe; touches NO network) ------------------
def selfcheck() -> bool:
    """Offline probe: a self-built FAKE opener drives a full poll→reconcile→report
    over an in-memory DB and asserts the verdict. Touches NO real network/token."""
    ace = "ACE"
    ch, mid = "C1", "M1"

    # poll 1: Ace ✅ present.
    def opener_present(req):
        return _FakeResp(req, {("M1", "✅"): [{"id": ace}]})
    db = ":memory:"
    conn = rs.connect(db)
    try:
        snap, fok = build_snapshot([(ch, mid)], ace, next_watermark(conn),
                                   token="x", opener=opener_present)
        covers, _ = compute_covers_removes(conn, [(ch, mid)], fok)
        snap["covers_removes"] = covers
        rs.reconcile(conn, snap, allow_removes=covers)
        v = _verdict_for_message(conn, ch, mid, ace)
        if v != "✅":
            return False

        # poll 2: Ace un-reacted (clean full poll) → verdict clears.
        snap2, fok2 = build_snapshot([(ch, mid)], ace, next_watermark(conn),
                                     token="x", opener=_opener_empty)
        covers2, _ = compute_covers_removes(conn, [(ch, mid)], fok2)
        snap2["covers_removes"] = covers2
        rs.reconcile(conn, snap2, allow_removes=covers2)
        if _verdict_for_message(conn, ch, mid, ace) is not None:
            return False

        # poll 3: Ace RE-reacts ✅ → verdict RECOVERS (the watermark guard's job).
        snap3, fok3 = build_snapshot([(ch, mid)], ace, next_watermark(conn),
                                     token="x", opener=opener_present)
        covers3, _ = compute_covers_removes(conn, [(ch, mid)], fok3)
        snap3["covers_removes"] = covers3
        rs.reconcile(conn, snap3, allow_removes=covers3)
        if _verdict_for_message(conn, ch, mid, ace) != "✅":
            return False
    finally:
        conn.close()
    return True


def _opener_empty(req):
    return _FakeResp(req, {})


class _FakeResp:
    """A urllib-opener stand-in for offline selfcheck. Maps (message_id, decoded
    emoji) → reactor list; everything else returns []."""

    def __init__(self, req, reaction_map):
        self._body, self._status = self._route(req.full_url, reaction_map)

    @staticmethod
    def _route(url, reaction_map):
        # .../messages/{mid}/reactions/{enc-emoji}?...
        path = url.split("?", 1)[0]
        parts = path.split("/")
        try:
            ri = parts.index("reactions")
            mid = parts[ri - 1]
            emoji = urllib.parse.unquote(parts[ri + 1])
        except (ValueError, IndexError):
            return b"[]", 200
        return json.dumps(reaction_map.get((mid, emoji), [])).encode(), 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run_selfcheck() -> int:
    try:
        ok = selfcheck()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"SELFCHECK FAIL: {exc}", file=sys.stderr)
        return 1
    if not ok:
        print("SELFCHECK FAIL: poll→reconcile→report logic violated", file=sys.stderr)
        return 1
    print("SELFCHECK OK")
    return 0


# --- --check-target (REST liveness; D-14: NOT the core's journal probe) ------
def check_target(ace_user_id, *, token=None, opener=_default_opener):
    """Assert the token is present AND a real GET /users/@me succeeds. This is the
    adapter's REST-liveness gate — it does NOT call the core's journal check_target
    (D-14). Returns (ok, message-with-visible-identities)."""
    try:
        me = api_get("/users/@me", token=token, opener=opener)
    except APIError as exc:
        return False, f"check-target FAIL: {exc}"
    bot_id = str(me.get("id", "?"))
    return True, (f"check-target OK: bot id={bot_id}, configured ace id={ace_user_id}")


def _run_check_target(ace_user_id, *, token=None, opener=_default_opener) -> int:
    ok, msg = check_target(ace_user_id, token=token, opener=opener)
    if not ok:
        print(msg, file=sys.stderr)
        return 2
    print(msg)
    return 0


# --- CLI --------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    # Shared options must be accepted BOTH before the subcommand
    # (`seed_triage --db X poll`) AND after it (`seed_triage poll --db X`, which is
    # what the spec's nightly entry writes). argparse parent-parsers re-apply their
    # OWN defaults on the subparser, which would clobber a top-level value with the
    # default — so the subcommand copy uses default=SUPPRESS (only present when the
    # user actually passes it) and we merge it over the top-level namespace.
    def add_shared(parser, *, suppress: bool):
        d = argparse.SUPPRESS
        parser.add_argument("--db", default=(d if suppress else str(
            Path.home() / ".hermes" / "greenhouse" / "seed_triage.db")))
        parser.add_argument("--seeds-dir",
                            default=(d if suppress else DEFAULT_SEEDS_DIR))
        parser.add_argument("--ace-user-id", default=(d if suppress else
                            os.environ.get("DISCORD_TRIAGE_USER_ID",
                                           DEFAULT_ACE_USER_ID)))
        parser.add_argument("--window-days", type=int,
                            default=(d if suppress else DEFAULT_WINDOW_DAYS))
        parser.add_argument("--json", action="store_true",
                            default=(d if suppress else False),
                            help="machine-readable report")

    p = argparse.ArgumentParser(
        prog="seed_triage",
        description="Discord-reaction → durable triage state adapter (v0.1).")
    add_shared(p, suppress=False)
    p.add_argument("--selfcheck", action="store_true",
                   help="offline logic probe (self-built fixture); NOT a liveness check")
    p.add_argument("--check-target", action="store_true",
                   help="REST liveness: token present AND GET /users/@me succeeds")
    sub = p.add_subparsers(dest="cmd")
    pp = sub.add_parser("poll",
                        help="poll Discord and fold reactions into durable state")
    add_shared(pp, suppress=True)
    pp.add_argument("--message", help="ad-hoc narrow poll of CHANNEL/MESSAGE (add-only)")
    rp = sub.add_parser("report", help="print the durable per-seed verdict table")
    add_shared(rp, suppress=True)
    args = p.parse_args(argv)

    if args.selfcheck:
        return _run_selfcheck()
    if args.check_target:
        return _run_check_target(args.ace_user_id)

    if args.cmd == "poll":
        seed_maps = load_seed_maps(args.seeds_dir)
        narrow = None
        if args.message:
            try:
                ch, mid = args.message.split("/", 1)
            except ValueError:
                p.error("--message must be CHANNEL/MESSAGE")
            narrow = [(ch, mid)]
        try:
            stats = poll(args.db, seed_maps, args.ace_user_id, args.window_days,
                         narrow_targets=narrow)
        except _FlockBusy as exc:
            print(f"BUSY: {exc}", file=sys.stderr)
            return 3
        except APIError as exc:
            print(f"POLL FAILED: {exc}", file=sys.stderr)
            return 1
        print(f"targets={stats['targets']} polled={stats['polled']} "
              f"fetch_fail={stats['fetch_fail']} ace_reactions={stats['ace_reactions']} "
              f"added={stats['added']} removed={stats['removed']} "
              f"covers_removes={stats['covers_removes']}")
        # POLL ANOMALY: 0 reachable targets when there is work to do.
        if stats["targets"] > 0 and stats["polled"] == 0:
            print("POLL ANOMALY: 0 reachable targets", file=sys.stderr)
            return 1
        # D-13: a FULL poll that disables removes names the dead key(s), loudly.
        if not stats["narrow"] and not stats["covers_removes"] and stats["uncovered_keys"]:
            keys = ", ".join(f"{c}/{m}" for c, m in stats["uncovered_keys"])
            print(f"COVERS_REMOVES_DISABLED: un-react removal is OFF until these "
                  f"durable key message(s) are reachable or purged: {keys}",
                  file=sys.stderr)
        return 0

    if args.cmd == "report":
        conn = rs.connect(args.db)
        try:
            seed_maps = load_seed_maps(args.seeds_dir)
            titles = load_seed_titles(args.seeds_dir)
            report(conn, seed_maps, titles, args.ace_user_id, args.window_days,
                   as_json=args.json)
        finally:
            conn.close()
        return 0

    p.error("a subcommand (poll | report) or --selfcheck / --check-target is required")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
