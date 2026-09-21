#!/usr/bin/env python3
"""
Nova autonomous MT5 DEMO trade executor.

Tails nova_signals.jsonl (written by the NovaSignals.mq5 expert advisor),
applies risk gates, and -- ONLY in live mode -- appends trade.open commands
to nova_commands.jsonl for the NovaTrader EA to execute.

HARD SAFETY RULES
  * DEMO ONLY. If any server string seen does not contain "demo"
    (case-insensitive), everything is refused and logged loudly.
  * The executor defaults to dry_run=true, trading_enabled=false. Effective
    "may trade new" = config trading_enabled AND kill-switch file
    run/trading_enabled containing "1" AND NOT dry_run.
  * In dry_run mode ZERO command bytes are written. Every decision is still
    journaled as "intended" with the full computed details (volume,
    risk_amount). (Dry run still needs fresh specs + sane sizing inputs to
    report those details; sizing failures journal the corresponding skip.)
  * No secrets flow through this script at all (there are none in this flow).

Env:
  MT5_FILES_DIR    dir containing nova_signals.jsonl / nova_commands.jsonl /
                   nova_trades.jsonl / nova_symbol_specs.json
                   (default: ~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Files)
  SIGNALS_FILE     filename override (default: nova_signals.jsonl)
  TRADER_STATE_DIR state dir for risk_config.json, kill switch, journal and
                   cursor files (default: <bridge dir>/run)

State (under TRADER_STATE_DIR):
  trade_executor.state.json   signal cursor + seen ids + trades cursor +
                              open_map + cmd_index + applied config signature
  risk_config.json            risk config; created with SAFE defaults if missing;
                              risk_per_trade_pct clamped to [0.25, 1.0],
                              max_concurrent_trades clamped to [1, 5]
  trading_enabled             kill switch ("1"/"0"; missing file = "0")
  nova_journal.jsonl          EVERY event: signal.received, trade.decision,
                              command.sent, trade.opened, trade.closed,
                              kill_switch.tripped, config.changed, reflection,
                              daily_summary

Usage:
  trade_executor.py [--iterations N]   run the daemon loop (default: forever)
"""

import json
import math
import os
import sys
import time
from datetime import datetime

import market_hours

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILES_DIR = os.path.expanduser(
    "~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Files"
)
DEFAULT_SIGNALS_FILE = "nova_signals.jsonl"

DEFAULT_CONFIG = {
    "trading_enabled": False,
    "dry_run": True,
    "risk_per_trade_pct": 0.5,
    "max_concurrent_trades": 3,
    "max_daily_loss_pct": 2.0,
    "max_spread_points": 50,
}

SPECS_MAX_AGE_S = 300

# Experiment risk bounds (EXPERIMENT_CHARTER.md). Values outside these bounds
# are clamped on load and the clamping is journaled as config.changed.
RISK_BOUNDS = {
    "risk_per_trade_pct": (0.25, 1.0),
    "max_concurrent_trades": (1, 5),
}


def apply_risk_bounds(cfg):
    """
    Clamp risk config values to RISK_BOUNDS; sanity-check the rest.
    Pure function. Returns (clamped_cfg, notes) where notes lists every
    change made (empty when nothing changed).
    """
    cfg = dict(cfg)
    notes = []
    for key, (lo, hi) in RISK_BOUNDS.items():
        try:
            v = float(cfg.get(key))
        except (TypeError, ValueError):
            v = None
        if v is None:
            continue
        clamped = min(max(v, lo), hi)
        if key == "max_concurrent_trades":
            clamped = int(clamped)
        if clamped != v:
            notes.append(f"{key} clamped {v} -> {clamped}")
            cfg[key] = clamped
    try:
        mdl = float(cfg.get("max_daily_loss_pct"))
    except (TypeError, ValueError):
        mdl = 0.0
    if not (0.0 < mdl <= 10.0):
        notes.append(
            f"max_daily_loss_pct {cfg.get('max_daily_loss_pct')} -> "
            f"{DEFAULT_CONFIG['max_daily_loss_pct']}")
        cfg["max_daily_loss_pct"] = DEFAULT_CONFIG["max_daily_loss_pct"]
    try:
        msp = float(cfg.get("max_spread_points"))
    except (TypeError, ValueError):
        msp = -1.0
    if msp < 0:
        notes.append(
            f"max_spread_points {cfg.get('max_spread_points')} -> "
            f"{DEFAULT_CONFIG['max_spread_points']}")
        cfg["max_spread_points"] = DEFAULT_CONFIG["max_spread_points"]
    return cfg, notes


def config_sig(cfg):
    """Stable signature of the effective risk config (for change detection)."""
    return json.dumps({k: cfg.get(k) for k in DEFAULT_CONFIG},
                      sort_keys=True)


# ---------------------------------------------------------------- paths

def resolve_paths(files_dir=None, state_dir=None, signals_file=None):
    fd = files_dir or os.environ.get("MT5_FILES_DIR", DEFAULT_FILES_DIR)
    sd = state_dir or os.environ.get(
        "TRADER_STATE_DIR", os.path.join(BASE, "run")
    )
    sf = signals_file or os.environ.get("SIGNALS_FILE", DEFAULT_SIGNALS_FILE)
    os.makedirs(sd, exist_ok=True)
    return {
        "files_dir": fd,
        "state_dir": sd,
        "signals": os.path.join(fd, sf),
        "commands": os.path.join(fd, "nova_commands.jsonl"),
        "trades": os.path.join(fd, "nova_trades.jsonl"),
        "specs": os.path.join(fd, "nova_symbol_specs.json"),
        "state": os.path.join(sd, "trade_executor.state.json"),
        "config": os.path.join(sd, "risk_config.json"),
        "kill": os.path.join(sd, "trading_enabled"),
        "journal": os.path.join(sd, "nova_journal.jsonl"),
    }


# ---------------------------------------------------------------- helpers

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"{now_str()} {msg}", flush=True)


def parse_mt5_time(s):
    """Parse MT5-style 'YYYY.MM.DD HH:MM:SS' or ISO-ish times. None on failure."""
    if not s:
        return None
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except ValueError:
            continue
    return None


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"offset": 0, "seen": [], "trades_offset": 0, "trades_seen": []}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def load_config(config_path):
    """Load risk_config.json, creating it with SAFE defaults if missing.
    Risk bounds are enforced (see apply_risk_bounds); clamping is logged."""
    if not os.path.exists(config_path):
        cfg = dict(DEFAULT_CONFIG)
        tmp = config_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, config_path)
        log(f"created {config_path} with safe defaults "
            f"(trading_enabled=false, dry_run=true)")
        return cfg
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        log(f"WARNING: could not read {config_path} ({e}); using safe defaults")
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg if isinstance(cfg, dict) else {})
    merged, notes = apply_risk_bounds(merged)
    for n in notes:
        log(f"risk bound enforced: {n}")
    return merged


def kill_switch_on(kill_path):
    """Missing kill-switch file is treated as '0' (disabled)."""
    try:
        with open(kill_path) as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def trip_kill_switch(kill_path):
    """Write '0' to the kill-switch file (daily loss limit)."""
    tmp = kill_path + ".tmp"
    with open(tmp, "w") as f:
        f.write("0")
    os.replace(tmp, kill_path)


def is_demo_server(server):
    return "demo" in str(server or "").lower()


def step_decimals(step):
    try:
        s = repr(float(step))
    except (TypeError, ValueError):
        return 2
    if "." not in s:
        return 0
    return len(s.split(".")[1].rstrip("0")) or 0


# ---------------------------------------------------------------- pure logic

def compute_volume(equity, risk_pct, entry, stop_loss, spec):
    """
    Return (volume, risk_amount, error). error is None on success or one of
    "skipped:bad_specs", "skipped:volume_below_min".
    """
    risk_amount = equity * risk_pct / 100.0
    tick_size = spec.get("tick_size") or 0
    tick_value = spec.get("tick_value") or 0
    sl_dist = abs((entry or 0) - (stop_loss or 0))
    sl_ticks = sl_dist / tick_size if tick_size else 0
    if sl_ticks <= 0 or tick_value <= 0:
        return 0.0, risk_amount, "skipped:bad_specs"
    raw_volume = risk_amount / (sl_ticks * tick_value)
    step = spec.get("volume_step") or 0
    if step <= 0:
        return 0.0, risk_amount, "skipped:bad_specs"
    volume = math.floor(raw_volume / step) * step
    volume = round(volume, step_decimals(step))
    vmin = spec.get("volume_min") or 0
    vmax = spec.get("volume_max") or 0
    if vmax and volume > vmax:
        volume = vmax
    if vmin and volume < vmin:
        return 0.0, risk_amount, "skipped:volume_below_min"
    return volume, risk_amount, None


def load_trade_state(trades_path, today_str):
    """
    Parse nova_trades.jsonl into open positions and today's closed P/L.
    Returns (open_positions, todays_profit) where open_positions maps
    ticket -> {"symbol", "command_id", "signal_id"}.
    """
    opened = {}
    closed_tickets = set()
    closed = []
    try:
        with open(trades_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                t = ev.get("type")
                if t == "trade.opened":
                    ticket = ev.get("ticket")
                    if ticket is not None:
                        opened[ticket] = {
                            "symbol": ev.get("symbol"),
                            "command_id": ev.get("command_id"),
                            "signal_id": ev.get("signal_id"),
                        }
                elif t == "trade.closed":
                    ticket = ev.get("ticket")
                    if ticket is not None:
                        closed_tickets.add(ticket)
                    closed.append(ev)
                # trade.rejected opens nothing
    except OSError:
        pass
    open_positions = {
        t: info for t, info in opened.items() if t not in closed_tickets
    }
    todays_profit = 0.0
    for ev in closed:
        dt = parse_mt5_time(ev.get("time"))
        ev_date = dt.date().isoformat() if dt else None
        if ev_date == today_str:
            try:
                todays_profit += float(ev.get("profit") or 0)
            except (TypeError, ValueError):
                pass
    return open_positions, todays_profit


def server_today_str(specs):
    """Today's date per the broker server clock (specs), else local date."""
    acct = (specs or {}).get("account") or {}
    for key in ("time",):
        dt = parse_mt5_time(acct.get(key)) or parse_mt5_time((specs or {}).get("time"))
        if dt:
            return dt.date().isoformat()
    return datetime.now().date().isoformat()


def check_gates(sig, *, enabled, dry_run, specs, specs_fresh,
                open_positions, todays_profit, today_str,
                risk_pct, max_concurrent, max_spread_points,
                max_daily_loss_pct, equity, market_open=True):
    """
    Run the risk gates in order; first failure wins.
    Returns (decision, flags) where flags may carry loud/trip side effects.
    Pure function: no I/O.
    """
    symbol = sig.get("symbol")
    entry = sig.get("entry_price")
    sl = sig.get("stop_loss")

    # 1. not enabled (config or kill switch)
    if not enabled:
        return "skipped:trading_disabled", {}

    # sizing needs specs; in dry_run we still want full computed details,
    # so stale/missing specs and sizing failures journal their skip reason.
    spec = (specs.get("symbols") or {}).get(symbol) if specs else None
    if not specs_fresh or spec is None:
        return "skipped:stale_specs", {}
    volume, risk_amount, err = compute_volume(equity, risk_pct, entry, sl, spec)
    if err:
        return err, {}

    # 2. dry_run -> intended, write nothing
    if dry_run:
        return "intended", {"volume": volume, "risk_amount": risk_amount}

    # 3. market closed -> no NEW positions (existing positions are held)
    if not market_open:
        return "skipped:market_closed", {}

    # 4. server not demo -> refuse loudly
    server = ((specs.get("account") or {}).get("server")
              or specs.get("server") or "")
    if not is_demo_server(server):
        return "skipped:non_demo_server", {"loud": True}

    # 5. max concurrent trades
    if len(open_positions) >= max_concurrent:
        return "skipped:max_concurrent", {}

    # 6. symbol already has an open position
    for info in open_positions.values():
        if info.get("symbol") == symbol:
            return "skipped:symbol_already_open", {}

    # 7. spread too wide
    spread = spec.get("spread_points")
    if spread is not None and spread > max_spread_points:
        return "skipped:spread_too_wide", {}

    # 8. daily loss limit
    daily_loss = 0.0 - todays_profit
    if daily_loss >= (max_daily_loss_pct / 100.0) * equity:
        return "skipped:daily_loss_limit", {"loud": True, "trip": True}

    return "commanded", {"volume": volume, "risk_amount": risk_amount}


def build_command(sig, volume):
    """Command dict for the NovaTrader EA (live mode only)."""
    return {
        "type": "trade.open",
        "id": f"cmd-{sig.get('id')}",
        "symbol": sig.get("symbol"),
        "direction": sig.get("direction"),
        "volume": volume,
        "sl": sig.get("stop_loss"),
        "tp": sig.get("take_profit"),
        "signal_id": sig.get("id"),
    }


def server_market_state(specs):
    """
    Return (market_open, context) for journaling at decision time.
    Server clock comes from specs account time (GMT+3); falls back to
    now-in-GMT+3 when unparsable.
    """
    server_time_str = ((specs or {}).get("account") or {}).get("time")
    server_dt = parse_mt5_time(server_time_str)
    if server_dt is None:
        market_open = market_hours.is_market_open()
        ctx = market_hours.market_context()
    else:
        market_open = market_hours.is_market_open(server_dt)
        ctx = {
            "server_time": server_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "server_hour": server_dt.hour,
            "weekday": server_dt.strftime("%a"),
            "session": market_hours.session_bucket(server_dt.hour),
            "market_open": market_open,
        }
    return market_open, ctx


# ---------------------------------------------------------------- engine

class TraderEngine:
    def __init__(self, files_dir=None, state_dir=None, signals_file=None):
        self.paths = resolve_paths(files_dir, state_dir, signals_file)
        self.state = load_state(self.paths["state"])
        # Guarantee the commands file exists (empty) at startup. The EA
        # initializes a missing cursor to EOF on first sight of the file;
        # if the file were created later already holding a command, that
        # command would be skipped. Creating it now (empty) makes the EA
        # anchor its cursor at 0=EOF before any command exists.
        try:
            if not os.path.exists(self.paths["commands"]):
                open(self.paths["commands"], "a").close()
        except OSError as e:
            log(f"warning: could not create commands file: {e}")

    # -- journal / state -------------------------------------------------
    def journal(self, entry):
        entry = dict(entry)
        entry.setdefault("time", now_str())
        with open(self.paths["journal"], "a") as f:
            f.write(json.dumps(entry) + "\n")

    def save(self):
        save_state(self.paths["state"], self.state)

    # -- config / inputs -------------------------------------------------
    def load_specs(self):
        """Return (specs_dict_or_None, fresh_bool)."""
        try:
            with open(self.paths["specs"]) as f:
                specs = json.load(f)
        except (OSError, ValueError):
            return None, False
        dt = parse_mt5_time(specs.get("time"))
        fresh = bool(dt and (datetime.now() - dt).total_seconds() <= SPECS_MAX_AGE_S)
        return specs, fresh

    def _open_info_for(self, ev):
        """Best-known open details for a trade.opened event (EA fields win,
        executor command index fills the gaps)."""
        cmd_id = ev.get("command_id")
        known = self.state.get("cmd_index", {}).get(cmd_id or "", {})
        return {
            "ticket": ev.get("ticket"),
            "deal": ev.get("deal"),
            "symbol": ev.get("symbol") or known.get("symbol"),
            "direction": ev.get("direction") or known.get("direction"),
            "volume": ev.get("volume") if ev.get("volume") is not None
                      else known.get("volume"),
            "entry_price": ev.get("fill_price"),
            "time": ev.get("time"),
            "signal_id": ev.get("signal_id") or known.get("signal_id"),
            "command_id": cmd_id,
        }

    # -- trades tail -> journal linkage ----------------------------------
    def tail_trades(self):
        p = self.paths["trades"]
        try:
            size = os.path.getsize(p)
        except OSError:
            return 0
        offset = self.state.get("trades_offset", 0)
        if size < offset:
            offset = 0
        # Dedup keys are plain strings (JSON-stable). Older states may hold
        # the key as a list (a tuple before the JSON round-trip); migrate.
        seen = set()
        for k in self.state.get("trades_seen", []):
            if isinstance(k, (list, tuple)):
                k = "|".join("" if v is None else str(v) for v in k)
            seen.add(k)
        open_map = self.state.get("open_map", {})
        cmd_index = self.state.get("cmd_index", {})
        linked = 0
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            while True:
                line_start = f.tell()
                line = f.readline()
                if not line:
                    offset = f.tell()
                    break
                line = line.strip()
                if not line:
                    offset = f.tell()
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    offset = f.tell()
                    continue
                t = ev.get("type")
                key = "|".join("" if v is None else str(v) for v in (
                    t, ev.get("command_id"), ev.get("ticket"),
                    ev.get("deal")))
                if key in seen:
                    offset = f.tell()
                    continue
                events = []
                if t == "trade.opened":
                    info = self._open_info_for(ev)
                    events.append({
                        "type": "trade.update",
                        "command_id": ev.get("command_id"),
                        "ticket": ev.get("ticket"),
                        "deal": ev.get("deal"),
                        "fill_price": ev.get("fill_price"),
                        "signal_id": info.get("signal_id"),
                    })
                    events.append({"type": "trade.opened", **info})
                    open_map[str(ev.get("ticket"))] = info
                    log(f"journal linked {t} "
                        f"ticket={ev.get('ticket')} cmd={ev.get('command_id')}")
                elif t == "trade.closed":
                    info = open_map.pop(str(ev.get("ticket")), {})
                    closed_dt = parse_mt5_time(ev.get("time"))
                    opened_dt = parse_mt5_time(info.get("time"))
                    hold_s = None
                    if closed_dt and opened_dt:
                        hold_s = max(
                            0, int((closed_dt - opened_dt).total_seconds()))
                    events.append({
                        "type": "trade.update",
                        "ticket": ev.get("ticket"),
                        "exit_price": ev.get("exit_price"),
                        "profit": ev.get("profit"),
                        "reason": ev.get("reason"),
                    })
                    events.append({
                        "type": "trade.closed",
                        "ticket": ev.get("ticket"),
                        "symbol": info.get("symbol"),
                        "direction": info.get("direction"),
                        "volume": info.get("volume"),
                        "entry_price": info.get("entry_price"),
                        "exit_price": ev.get("exit_price"),
                        "profit": ev.get("profit"),
                        "hold_seconds": hold_s,
                        "reason": ev.get("reason"),
                        "time": ev.get("time"),
                        "signal_id": info.get("signal_id"),
                        "command_id": info.get("command_id"),
                    })
                    log(f"journal linked {t} ticket={ev.get('ticket')} "
                        f"profit={ev.get('profit')}")
                elif t == "trade.rejected":
                    events.append({
                        "type": "trade.update",
                        "command_id": ev.get("command_id"),
                        "rejected": ev.get("reason"),
                    })
                    log(f"journal linked {t} cmd={ev.get('command_id')} "
                        f"reason={ev.get('reason')}")
                for entry in events:
                    self.journal(entry)
                if events:
                    seen.add(key)
                    linked += 1
                offset = f.tell()
        self.state["trades_offset"] = offset
        self.state["trades_seen"] = sorted(seen, key=repr)[-500:]
        self.state["open_map"] = dict(list(open_map.items())[-200:])
        self.state["cmd_index"] = dict(list(cmd_index.items())[-500:])
        self.save()
        return linked

    # -- one signal -------------------------------------------------------
    def decide(self, sig, cfg, kill_on, specs, specs_fresh,
               open_positions, todays_profit, today_str, market_open=True):
        enabled = bool(cfg.get("trading_enabled")) and kill_on
        decision, flags = check_gates(
            sig,
            enabled=enabled,
            dry_run=bool(cfg.get("dry_run")),
            specs=specs,
            specs_fresh=specs_fresh,
            open_positions=open_positions,
            todays_profit=todays_profit,
            today_str=today_str,
            risk_pct=cfg.get("risk_per_trade_pct", 0.5),
            max_concurrent=cfg.get("max_concurrent_trades", 3),
            max_spread_points=cfg.get("max_spread_points", 50),
            max_daily_loss_pct=cfg.get("max_daily_loss_pct", 2.0),
            equity=((specs.get("account") or {}).get("equity") or 0)
                   if specs else 0,
            market_open=market_open,
        )
        return decision, flags

    def handle_signal(self, sig, cfg, kill_on, specs, specs_fresh,
                      open_positions, todays_profit, today_str,
                      market_open=True, context=None):
        decision, flags = self.decide(
            sig, cfg, kill_on, specs, specs_fresh,
            open_positions, todays_profit, today_str, market_open,
        )
        entry = {
            "type": "trade.decision",
            "signal_id": sig.get("id"),
            "symbol": sig.get("symbol"),
            "direction": sig.get("direction"),
            "decision": decision,
            "volume": flags.get("volume"),
            "risk_amount": flags.get("risk_amount"),
            "context": context or {},
        }
        if flags.get("loud"):
            log(f"!!! REFUSAL: {decision} for {sig.get('id')} "
                f"({sig.get('symbol')} {sig.get('direction')})")
        if flags.get("trip"):
            trip_kill_switch(self.paths["kill"])
            self.journal({
                "type": "kill_switch.tripped",
                "reason": "daily_loss_limit",
                "signal_id": sig.get("id"),
                "todays_profit": todays_profit,
                "max_daily_loss_pct": cfg.get("max_daily_loss_pct"),
            })
            log("!!! DAILY LOSS LIMIT HIT - kill switch set to 0, "
                "trading halted")
        if decision == "commanded":
            cmd = build_command(sig, flags["volume"])
            with open(self.paths["commands"], "a") as f:
                f.write(json.dumps(cmd) + "\n")
            entry["command_id"] = cmd["id"]
            cmd_index = self.state.get("cmd_index", {})
            cmd_index[cmd["id"]] = {
                "signal_id": sig.get("id"),
                "symbol": sig.get("symbol"),
                "direction": sig.get("direction"),
                "volume": flags["volume"],
                "entry_price": sig.get("entry_price"),
            }
            self.state["cmd_index"] = dict(list(cmd_index.items())[-500:])
            self.journal({"type": "command.sent", "command": cmd})
            log(f"COMMANDED {cmd['id']}: {cmd['direction']} {cmd['volume']} "
                f"{cmd['symbol']} sl={cmd['sl']} tp={cmd['tp']}")
        elif decision == "intended":
            log(f"INTENDED (dry run): {sig.get('direction')} "
                f"{flags.get('volume')} {sig.get('symbol')} "
                f"risk=${flags.get('risk_amount')} "
                f"sl={sig.get('stop_loss')} tp={sig.get('take_profit')} "
                f"[no command written]")
        else:
            log(f"SKIPPED {decision}: {sig.get('id')} "
                f"({sig.get('symbol')} {sig.get('direction')})")
        self.journal(entry)
        return decision

    # -- signals tail ----------------------------------------------------
    def process_signals(self):
        cfg = load_config(self.paths["config"])
        kill_on = kill_switch_on(self.paths["kill"])
        specs, specs_fresh = self.load_specs()
        today_str = server_today_str(specs)
        market_open, ctx = server_market_state(specs)
        open_positions, todays_profit = load_trade_state(
            self.paths["trades"], today_str
        )

        # config change detection: journal when effective risk config changes
        sig_now = config_sig(cfg)
        old_sig = self.state.get("config_sig")
        old_cfg = self.state.get("config_applied")
        if old_sig is not None and old_sig != sig_now:
            self.journal({"type": "config.changed", "old": old_cfg,
                          "new": {k: cfg.get(k) for k in DEFAULT_CONFIG}})
            log(f"config changed: {old_cfg} -> "
                f"{ {k: cfg.get(k) for k in DEFAULT_CONFIG} }")
        self.state["config_sig"] = sig_now
        self.state["config_applied"] = {k: cfg.get(k)
                                        for k in DEFAULT_CONFIG}
        self.save()

        try:
            size = os.path.getsize(self.paths["signals"])
        except OSError:
            return []
        offset = self.state.get("offset", 0)
        if size < offset:
            offset = 0
        seen = set(self.state.get("seen", []))
        decisions = []
        with open(self.paths["signals"], "r",
                  encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            while True:
                line = f.readline()
                if not line:
                    offset = f.tell()
                    break
                stripped = line.strip()
                if not stripped:
                    offset = f.tell()
                    continue
                try:
                    sig = json.loads(stripped)
                except ValueError:
                    offset = f.tell()
                    continue
                if sig.get("type") != "signal.detected":
                    offset = f.tell()
                    continue
                sid = (sig.get("id")
                       or f"{sig.get('symbol')}-{sig.get('candle_time')}-"
                          f"{sig.get('direction')}")
                if sid in seen:
                    offset = f.tell()
                    continue
                seen.add(sid)
                self.journal({
                    "type": "signal.received",
                    "signal_id": sid,
                    "symbol": sig.get("symbol"),
                    "timeframe": sig.get("timeframe"),
                    "direction": sig.get("direction"),
                    "entry_price": sig.get("entry_price"),
                    "stop_loss": sig.get("stop_loss"),
                    "take_profit": sig.get("take_profit"),
                    "candle_time": sig.get("candle_time"),
                    "server_time": sig.get("server_time"),
                    "trigger": sig.get("trigger"),
                    "rsi": sig.get("rsi_value"),
                    "atr": sig.get("atr_value"),
                })
                # refresh kill switch + trade state per signal so a trip
                # mid-batch takes effect immediately
                kill_on = kill_switch_on(self.paths["kill"])
                open_positions, todays_profit = load_trade_state(
                    self.paths["trades"], today_str
                )
                spec = ((specs.get("symbols") or {}).get(sig.get("symbol"))
                        if specs else None)
                context = dict(ctx)
                context["spread_points"] = (spec or {}).get("spread_points")
                decisions.append(self.handle_signal(
                    sig, cfg, kill_on, specs, specs_fresh,
                    open_positions, todays_profit, today_str,
                    market_open, context,
                ))
                offset = f.tell()
        self.state["offset"] = offset
        self.state["seen"] = sorted(seen)[-500:]
        self.save()
        return decisions

    # -- main pass --------------------------------------------------------
    def run_once(self):
        self.tail_trades()
        return self.process_signals()


def main(argv):
    iterations = None
    if "--iterations" in argv:
        try:
            iterations = int(argv[argv.index("--iterations") + 1])
        except (IndexError, ValueError):
            print("--iterations needs an integer", file=sys.stderr)
            return 2
    eng = TraderEngine()
    cfg = load_config(eng.paths["config"])
    log(f"trade executor watching {eng.paths['signals']}")
    log(f"config: trading_enabled={cfg.get('trading_enabled')} "
        f"dry_run={cfg.get('dry_run')} "
        f"risk={cfg.get('risk_per_trade_pct')}% "
        f"max_concurrent={cfg.get('max_concurrent_trades')} "
        f"max_daily_loss={cfg.get('max_daily_loss_pct')}% "
        f"max_spread={cfg.get('max_spread_points')}pts")
    if cfg.get("dry_run"):
        log("DRY RUN MODE: zero commands will be written; "
            "decisions journaled as 'intended'")
    if not cfg.get("trading_enabled"):
        log("trading disabled in config: signals will journal "
            "'skipped:trading_disabled'")
    n = 0
    while True:
        try:
            eng.run_once()
        except Exception as e:
            log(f"loop error: {e}")
        n += 1
        if iterations is not None and n >= iterations:
            break
        time.sleep(5)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
