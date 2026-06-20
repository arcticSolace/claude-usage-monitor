#!/usr/bin/env python3
"""

        Claude Code - Usage & Status Monitor                       
        Reads local JSONL logs + Anthropic Status API              

"""

import os
import json
import glob
import time
import shutil
import urllib.request
import urllib.error
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

#  ANSI colours 
class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    # Foreground
    BLACK   = "\033[30m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"
    BWHITE  = "\033[97m"
    # Bright foreground
    BRED    = "\033[91m"
    BGREEN  = "\033[92m"
    BYELLOW = "\033[93m"
    BBLUE   = "\033[94m"
    BMAGENTA= "\033[95m"
    BCYAN   = "\033[96m"
    # Background
    BG_BLACK = "\033[40m"
    BG_RED   = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW= "\033[43m"
    BG_BLUE  = "\033[44m"
    BG_CYAN  = "\033[46m"

def no_color(s):
    """Strip ANSI codes for width calculations."""
    import re
    return re.sub(r'\033\[[0-9;]*m', '', s)

def visible_len(s):
    return len(no_color(s))

#  Token limits per plan 
PLAN_LIMITS = {
    "pro":   {"session": 44_000,  "weekly": 308_000,  "label": "Pro"},
    "max5":  {"session": 88_000,  "weekly": 616_000,  "label": "Max 5"},
    "max20": {"session": 220_000, "weekly": 1_540_000, "label": "Max 20"},
    "api":   {"session": None,    "weekly": None,      "label": "API (pay-per-use)"},
}
SESSION_WINDOW_HOURS = 5

#  ASCII Banner 
BANNER = f"""{C.BBLUE}{C.BOLD}
  ██████╗██╗      █████╗ ██╗  ██╗ ██████╗ ███████╗
 ██╔════╝██║     ██╔══██╗██║  ██║ ██╔══██╗██╔════╝
 ██║     ██║     ███████║██║  ██║ ██║  ██║█████╗
 ██║     ██║     ██╔══██║██║  ██║ ██║  ██║██╔══╝
 ╚██████╗███████╗██║  ██║╚██████╝ ██████╔╝███████╗
  ╚═════╝╚══════╝╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚══════╝{C.RESET}
{C.CYAN}{C.BOLD}             C O D E      U S A G E   &   S T A T U S{C.RESET}
{C.DIM}         Reads ~/.claude JSONL logs + status.anthropic.com{C.RESET}"""

#  Helpers 
def term_width():
    return shutil.get_terminal_size((100, 40)).columns

def separator(char="─", color=C.DIM):
    w = term_width()
    print(f"{color}{char * w}{C.RESET}")

def section_header(title, color=C.BCYAN):
    w = term_width()
    pad = max(0, w - visible_len(title) - 4)
    print(f"\n{color}{C.BOLD}   {title}{C.RESET}{C.DIM}{' ' + '─' * (pad - 1)}{C.RESET}")

def progress_bar(pct, width=40, color_warn=75, color_crit=90):
    filled = int(width * pct / 100)
    empty  = width - filled
    if pct >= color_crit:
        bar_color = C.BRED
    elif pct >= color_warn:
        bar_color = C.BYELLOW
    else:
        bar_color = C.BGREEN

    bar = f"{bar_color}{'█' * filled}{C.DIM}{'░' * empty}{C.RESET}"
    return bar

def fmt_tokens(n):
    if n is None:
        return "N/A"
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def status_icon(status):
    status = (status or "").lower()
    if status == "operational":
        return f"{C.BGREEN}●{C.RESET}", f"{C.BGREEN}Operational{C.RESET}"
    if status in ("degraded_performance", "partial_outage"):
        return f"{C.BYELLOW}●{C.RESET}", f"{C.BYELLOW}{status.replace('_',' ').title()}{C.RESET}"
    if status == "major_outage":
        return f"{C.BRED}●{C.RESET}", f"{C.BRED}Major Outage{C.RESET}"
    if status == "under_maintenance":
        return f"{C.BCYAN}●{C.RESET}", f"{C.BCYAN}Maintenance{C.RESET}"
    return f"{C.DIM}?{C.RESET}", f"{C.DIM}{status or 'Unknown'}{C.RESET}"

#  JSONL parsing 
def find_jsonl_files():
    """Return list of all session JSONL file paths."""
    paths = []
    base = Path.home() / ".claude" / "projects"
    if base.exists():
        # top-level JSONL only (skip subagent sub-dirs)
        paths += glob.glob(str(base / "*" / "*.jsonl"))
    # Statusline snapshots
    sl = Path.home() / ".claude" / "statusline.jsonl"
    return paths, sl

def parse_session_tokens(jsonl_files, window_hours=SESSION_WINDOW_HOURS):
    """
    Parse tokens from current 5-hour session window.
    Returns (input_tok, output_tok, cache_read, cache_write, model, session_start_ts).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    total_input = total_output = cache_read = cache_write = 0
    model_used  = "unknown"
    session_start = None
    files_found = len(jsonl_files)

    for path in jsonl_files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts_str = rec.get("timestamp")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue

                    if ts < cutoff:
                        continue

                    # Track session start
                    if session_start is None or ts < session_start:
                        session_start = ts

                    # Get usage from assistant messages
                    usage = None
                    msg = rec.get("message", {})
                    if isinstance(msg, dict):
                        usage = msg.get("usage")
                        m = msg.get("model")
                        if m and m != "unknown":
                            model_used = m
                    if usage is None:
                        usage = rec.get("usage")

                    if isinstance(usage, dict):
                        total_input  += usage.get("input_tokens", 0)
                        total_output += usage.get("output_tokens", 0)
                        cache_read   += usage.get("cache_read_input_tokens", 0)
                        cache_write  += usage.get("cache_creation_input_tokens", 0)
        except (OSError, PermissionError):
            continue

    return {
        "input":        total_input,
        "output":       total_output,
        "cache_read":   cache_read,
        "cache_write":  cache_write,
        "total":        total_input + total_output,
        "model":        model_used,
        "session_start":session_start,
        "files_scanned":files_found,
    }

def parse_weekly_tokens(jsonl_files):
    """Sum tokens for the current calendar week (MonSun)."""
    now  = datetime.now(timezone.utc)
    week_start = now - timedelta(days=now.weekday())  # Monday 00:00
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

    total_input = total_output = 0
    for path in jsonl_files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts_str = rec.get("timestamp")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts < week_start:
                        continue

                    usage = None
                    msg = rec.get("message", {})
                    if isinstance(msg, dict):
                        usage = msg.get("usage")
                    if usage is None:
                        usage = rec.get("usage")
                    if isinstance(usage, dict):
                        total_input  += usage.get("input_tokens", 0)
                        total_output += usage.get("output_tokens", 0)
        except (OSError, PermissionError):
            continue

    return {
        "input":  total_input,
        "output": total_output,
        "total":  total_input + total_output,
        "week_start": week_start,
    }

def read_statusline(sl_path):
    """Read the latest entry from ~/.claude/statusline.jsonl."""
    if not sl_path.exists():
        return None
    last = None
    try:
        with open(sl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        last = json.loads(line)
                    except json.JSONDecodeError:
                        pass
    except (OSError, PermissionError):
        pass
    return last

#  Anthropic Status API 
STATUS_URL = "https://status.anthropic.com/api/v2/summary.json"

COMPONENT_NAMES = {
    "claude api": "Claude API",
    "api":        "Claude API",
    "claude code":"Claude Code",
    "claude.ai":  "claude.ai",
}

def fetch_status(timeout=8):
    """Fetch status summary from Anthropic's statuspage API."""
    try:
        req = urllib.request.Request(
            STATUS_URL,
            headers={"User-Agent": "claude-usage-monitor/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

def extract_components(data):
    """Return dict: component_name  status for key services."""
    if not data:
        return {}
    result = {}
    for comp in data.get("components", []):
        name   = comp.get("name", "")
        status = comp.get("status", "unknown")
        nl = name.lower()
        if "claude api" in nl or nl == "api":
            result["Claude API"] = status
        elif "claude code" in nl:
            result["Claude Code"] = status
        elif "claude.ai" in nl:
            result["claude.ai"] = status
        elif "console" in nl or "platform" in nl:
            result["Console"] = status
    return result

def extract_incidents(data, limit=5):
    """Return recent unresolved + resolved incidents."""
    if not data:
        return [], []

    unresolved = []
    resolved   = []

    for inc in data.get("incidents", []):
        name    = inc.get("name", "Unknown incident")
        status  = inc.get("status", "")
        impact  = inc.get("impact", "none")
        started = inc.get("created_at", "")
        updated = inc.get("updated_at", "")

        # Latest update text
        updates = inc.get("incident_updates", [])
        latest_body = updates[0].get("body", "") if updates else ""

        entry = {
            "name":    name,
            "status":  status,
            "impact":  impact,
            "started": started,
            "updated": updated,
            "latest":  latest_body[:120] + ("…" if len(latest_body) > 120 else ""),
        }

        if status in ("resolved", "postmortem"):
            resolved.append(entry)
        else:
            unresolved.append(entry)

    return unresolved[:limit], resolved[:limit]

def fmt_ts(ts_str):
    """Format ISO timestamp to human-readable local-ish string."""
    if not ts_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return ts_str[:16]

def impact_color(impact):
    m = {"critical": C.BRED, "major": C.BRED, "minor": C.BYELLOW, "none": C.DIM}
    return m.get((impact or "").lower(), C.DIM)

#  Display functions 
def print_banner():
    os.system("cls" if os.name == "nt" else "clear")
    print(BANNER)
    separator("─", C.BBLUE)

def print_session(sess, plan_limits):
    section_header("CURRENT SESSION  (5-hour rolling window)")

    total    = sess["total"]
    limit    = plan_limits["session"]
    start    = sess["session_start"]
    model    = sess["model"]
    scanned  = sess["files_scanned"]

    now = datetime.now(timezone.utc)
    if start:
        elapsed   = now - start
        el_h, rem = divmod(int(elapsed.total_seconds()), 3600)
        el_m      = rem // 60
        remaining_s = max(0, SESSION_WINDOW_HOURS * 3600 - int(elapsed.total_seconds()))
        rem_h, rem2 = divmod(remaining_s, 3600)
        rem_m        = rem2 // 60
        elapsed_str  = f"{el_h}h {el_m:02d}m"
        remaining_str= f"{rem_h}h {rem_m:02d}m"
    else:
        elapsed_str = remaining_str = "N/A"

    # Token breakdown
    print(f"\n  {C.DIM}Model:{C.RESET}    {C.BCYAN}{model}{C.RESET}   "
          f"{C.DIM}Files scanned:{C.RESET} {scanned}")
    print(f"  {C.DIM}Started:{C.RESET}  {start.strftime('%H:%M:%S UTC') if start else 'No active session'}"
          f"   {C.DIM}Elapsed:{C.RESET} {elapsed_str}   {C.DIM}Remaining:{C.RESET} {remaining_str}\n")

    rows = [
        ("Input tokens",        sess["input"]),
        ("Output tokens",       sess["output"]),
        ("Cache reads",         sess["cache_read"]),
        ("Cache writes",        sess["cache_write"]),
        ("Total tokens used",   total),
    ]
    for label, val in rows:
        bold = C.BOLD if label.startswith("Total") else ""
        color = C.BWHITE if label.startswith("Total") else C.WHITE
        print(f"  {bold}{color}{label:<22}{C.RESET}  {C.BYELLOW}{fmt_tokens(val):>8}{C.RESET}")

    # Progress bar
    if limit:
        pct = min(100.0, total / limit * 100)
        bar = progress_bar(pct)
        limit_color = C.BRED if pct >= 90 else (C.BYELLOW if pct >= 75 else C.BGREEN)
        print(f"\n  {C.DIM}Session limit ({fmt_tokens(limit)}):{C.RESET}")
        print(f"  {bar}  {limit_color}{C.BOLD}{pct:5.1f}%{C.RESET}  "
              f"{C.DIM}({fmt_tokens(total)} / {fmt_tokens(limit)}){C.RESET}")
    else:
        print(f"\n  {C.DIM}Session limit: not applicable for API/pay-per-use plans{C.RESET}")

def print_weekly(weekly, plan_limits):
    section_header("WEEKLY LIMIT  (Mon – Sun calendar week)")

    total = weekly["total"]
    limit = plan_limits["weekly"]
    ws    = weekly["week_start"]

    print(f"\n  {C.DIM}Week from:{C.RESET}  {ws.strftime('%Y-%m-%d (Monday) UTC')}\n")
    rows = [
        ("Input tokens",  weekly["input"]),
        ("Output tokens", weekly["output"]),
        ("Total this week", total),
    ]
    for label, val in rows:
        bold  = C.BOLD if label.startswith("Total") else ""
        color = C.BWHITE if label.startswith("Total") else C.WHITE
        print(f"  {bold}{color}{label:<22}{C.RESET}  {C.BYELLOW}{fmt_tokens(val):>8}{C.RESET}")

    if limit:
        pct = min(100.0, total / limit * 100)
        bar = progress_bar(pct)
        limit_color = C.BRED if pct >= 90 else (C.BYELLOW if pct >= 75 else C.BGREEN)
        print(f"\n  {C.DIM}Weekly limit ({fmt_tokens(limit)}):{C.RESET}")
        print(f"  {bar}  {limit_color}{C.BOLD}{pct:5.1f}%{C.RESET}  "
              f"{C.DIM}({fmt_tokens(total)} / {fmt_tokens(limit)}){C.RESET}")
    else:
        print(f"\n  {C.DIM}Weekly limit: not applicable for API/pay-per-use plans{C.RESET}")

def print_service_status(components, overall_status):
    section_header("SERVICE STATUS")
    print()

    # Overall page indicator
    ov_ind = overall_status.get("indicator", "none")
    ov_desc= overall_status.get("description", "")
    if ov_ind == "none":
        ov_str = f"{C.BGREEN}{C.BOLD}●  {ov_desc}{C.RESET}"
    elif ov_ind == "minor":
        ov_str = f"{C.BYELLOW}{C.BOLD}●  {ov_desc}{C.RESET}"
    else:
        ov_str = f"{C.BRED}{C.BOLD}●  {ov_desc}{C.RESET}"
    print(f"  Overall:    {ov_str}\n")

    # Per-component rows
    target_keys = ["Claude API", "Claude Code", "claude.ai", "Console"]
    for key in target_keys:
        status = components.get(key)
        if status is None:
            continue
        icon, label = status_icon(status)
        print(f"  {icon}  {C.BOLD}{key:<18}{C.RESET}  {label}")

def print_incidents(unresolved, resolved):
    section_header("INCIDENT ALERTS")

    if not unresolved and not resolved:
        print(f"\n  {C.BGREEN}●  No incidents to report.{C.RESET}\n")
        return

    if unresolved:
        print(f"\n  {C.BRED}{C.BOLD}●  ACTIVE INCIDENTS ({len(unresolved)}){C.RESET}")
        for inc in unresolved:
            ic = impact_color(inc["impact"])
            print(f"\n  {ic}●  {inc['name']}{C.RESET}")
            print(f"    {C.DIM}Status:{C.RESET}  {inc['status'].upper()}"
                  f"   {C.DIM}Impact:{C.RESET}  {inc['impact'].upper()}")
            print(f"    {C.DIM}Started:{C.RESET} {fmt_ts(inc['started'])}"
                  f"   {C.DIM}Updated:{C.RESET} {fmt_ts(inc['updated'])}")
            if inc["latest"]:
                print(f"    {C.DIM}Update:{C.RESET}  {C.WHITE}{inc['latest']}{C.RESET}")

    if resolved:
        print(f"\n  {C.DIM}RECENTLY RESOLVED ({len(resolved)}){C.RESET}")
        for inc in resolved[:3]:
            print(f"\n  {C.DIM}○  {inc['name']}{C.RESET}")
            print(f"    {C.DIM}Resolved at: {fmt_ts(inc['updated'])}{C.RESET}")
            if inc["latest"]:
                print(f"    {C.DIM}{inc['latest']}{C.RESET}")

def print_footer(plan_label, refreshed_at, auto_refresh=None):
    separator("─", C.DIM)
    ts = refreshed_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    ar = f"  {C.DIM}Auto-refresh: {auto_refresh}s{C.RESET}" if auto_refresh else ""
    print(f"  {C.DIM}Plan:{C.RESET} {C.BCYAN}{plan_label}{C.RESET}"
          f"   {C.DIM}Last update:{C.RESET} {C.BWHITE}{ts}{C.RESET}{ar}")
    print(f"  {C.DIM}Sources: ~/.claude/projects/*.jsonl  status.anthropic.com{C.RESET}")
    if auto_refresh:
        print(f"  {C.DIM}Press Ctrl+C to exit{C.RESET}")

#  Main render 
def render(plan, auto_refresh=None):
    plan_limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["pro"])

    # Gather data
    jsonl_files, sl_path = find_jsonl_files()
    sess   = parse_session_tokens(jsonl_files)
    weekly = parse_weekly_tokens(jsonl_files)
    sl     = read_statusline(sl_path)

    # Enrich session model from statusline if available
    if sl and sess["model"] == "unknown":
        sess["model"] = sl.get("model", "unknown")

    # Status API
    status_data = fetch_status()
    components  = {}
    overall     = {"indicator": "none", "description": "All Systems Operational"}
    unresolved  = []
    resolved_inc= []

    if status_data:
        components  = extract_components(status_data)
        overall_raw = status_data.get("status", {})
        overall     = {
            "indicator":   overall_raw.get("indicator", "none"),
            "description": overall_raw.get("description", "Unknown"),
        }
        unresolved, resolved_inc = extract_incidents(status_data)
    else:
        components = {"Claude API": "unknown", "Claude Code": "unknown"}
        overall    = {"indicator": "unknown", "description": "Could not reach status.anthropic.com"}

    now = datetime.now(timezone.utc)

    # Render
    print_banner()
    print_session(sess, plan_limits)
    print_weekly(weekly, plan_limits)
    print_service_status(components, overall)
    print_incidents(unresolved, resolved_inc)
    print()
    print_footer(plan_limits["label"], now, auto_refresh)
    print()

#  Entry point 
def main():
    parser = argparse.ArgumentParser(
        description="Claude Code Usage & Status Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Plans:
  pro     Claude Pro        (~44K tokens / 5-hour window)
  max5    Claude Max 5     (~88K tokens / 5-hour window)
  max20   Claude Max 20    (~220K tokens / 5-hour window)
  api     API / pay-per-use  (no hard token limits shown)

Examples:
  python claude_usage_monitor.py
  python claude_usage_monitor.py --plan max5
  python claude_usage_monitor.py --watch 30
  python claude_usage_monitor.py --plan max20 --watch 60
        """,
    )
    parser.add_argument(
        "--plan", choices=["pro", "max5", "max20", "api"],
        default="pro", help="Your Claude subscription plan (default: pro)"
    )
    parser.add_argument(
        "--watch", type=int, metavar="SECONDS",
        help="Auto-refresh interval in seconds (e.g. --watch 30)"
    )
    args = parser.parse_args()

    if args.watch:
        try:
            while True:
                render(args.plan, auto_refresh=args.watch)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print(f"\n{C.DIM}Monitor stopped.{C.RESET}\n")
    else:
        render(args.plan)

if __name__ == "__main__":
    main()
