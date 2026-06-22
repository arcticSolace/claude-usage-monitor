#!/usr/bin/env python3
"""

        Claude Code - Usage & Status Monitor
        Reads local JSONL logs + Anthropic Status API

"""

import os
import re
import sys
import json
import glob
import time
import shutil
import urllib.request
import urllib.error
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

__version__ = "1.0.0"

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
    return re.sub(r'\033\[[0-9;]*m', '', s)

def visible_len(s):
    return len(no_color(s))

#  Token limits per plan
#  Session limits: Anthropic rate-limits Claude Code in a 5-hour rolling window.
#  Monthly limits: estimated token equivalents; Anthropic publishes these as
#  "5× / 20× more usage than Pro" without stating exact token values.
#  Max 5× = 5× Pro, Max 20× = 20× Pro — limits reset on the monthly billing cycle.
PLAN_LIMITS = {
    "pro":   {"session": 44_000,    "monthly": 1_000_000,   "label": "Pro"},
    "max5":  {"session": 220_000,   "monthly": 5_000_000,   "label": "Max 5×"},
    "max20": {"session": 880_000,   "monthly": 20_000_000,  "label": "Max 20×"},
    "api":   {"session": None,      "monthly": None,         "label": "API (pay-per-use)"},
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
    return f"{bar_color}{'█' * filled}{C.DIM}{'░' * empty}{C.RESET}"

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
        return f"{C.BYELLOW}●{C.RESET}", f"{C.BYELLOW}{status.replace('_', ' ').title()}{C.RESET}"
    if status == "major_outage":
        return f"{C.BRED}●{C.RESET}", f"{C.BRED}Major Outage{C.RESET}"
    if status == "under_maintenance":
        return f"{C.BCYAN}●{C.RESET}", f"{C.BCYAN}Maintenance{C.RESET}"
    return f"{C.DIM}?{C.RESET}", f"{C.DIM}{status or 'Unknown'}{C.RESET}"

#  JSONL parsing
def find_jsonl_files():
    """Return (list_of_jsonl_paths, statusline_path)."""
    base = Path.home() / ".claude" / "projects"
    paths = glob.glob(str(base / "*" / "*.jsonl")) if base.exists() else []
    sl = Path.home() / ".claude" / "statusline.jsonl"
    return paths, sl

def iter_usage_records(jsonl_files, since=None):
    """Yield (record, timestamp) for each valid usage record at or after `since`."""
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
                    if since is not None and ts < since:
                        continue
                    yield rec, ts
        except (OSError, PermissionError):
            continue

def parse_session_tokens(jsonl_files, window_hours=SESSION_WINDOW_HOURS):
    """Parse tokens from the current rolling session window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    total_input = total_output = cache_read = cache_write = 0
    model_used  = "unknown"
    session_start = None

    for rec, ts in iter_usage_records(jsonl_files, since=cutoff):
        if session_start is None or ts < session_start:
            session_start = ts
        msg = rec.get("message", {})
        usage = None
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

    return {
        "input":         total_input,
        "output":        total_output,
        "cache_read":    cache_read,
        "cache_write":   cache_write,
        "total":         total_input + total_output,
        "model":         model_used,
        "session_start": session_start,
        "files_scanned": len(jsonl_files),
    }

def parse_monthly_tokens(jsonl_files):
    """Sum tokens for the current calendar month (1st → now)."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total_input = total_output = 0
    for rec, _ in iter_usage_records(jsonl_files, since=month_start):
        msg = rec.get("message", {})
        usage = None
        if isinstance(msg, dict):
            usage = msg.get("usage")
        if usage is None:
            usage = rec.get("usage")
        if isinstance(usage, dict):
            total_input  += usage.get("input_tokens", 0)
            total_output += usage.get("output_tokens", 0)
    return {
        "input":       total_input,
        "output":      total_output,
        "total":       total_input + total_output,
        "month_start": month_start,
    }

#  Anthropic Status API
STATUS_URL = "https://status.anthropic.com/api/v2/summary.json"

def fetch_status(timeout=8):
    """Fetch status summary from Anthropic's statuspage API."""
    try:
        req = urllib.request.Request(
            STATUS_URL,
            headers={"User-Agent": f"claude-usage-monitor/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

def extract_components(data):
    """Return dict of component_name → status for key services."""
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
    """Return (unresolved, resolved) incident lists, capped at limit / 3."""
    if not data:
        return [], []
    unresolved = []
    resolved   = []
    for inc in data.get("incidents", []):
        updates     = inc.get("incident_updates", [])
        latest_body = updates[0].get("body", "") if updates else ""
        entry = {
            "name":    inc.get("name", "Unknown incident"),
            "status":  inc.get("status", ""),
            "impact":  inc.get("impact", "none"),
            "started": inc.get("created_at", ""),
            "updated": inc.get("updated_at", ""),
            "latest":  latest_body[:120] + ("…" if len(latest_body) > 120 else ""),
        }
        if entry["status"] in ("resolved", "postmortem"):
            resolved.append(entry)
        else:
            unresolved.append(entry)
    return unresolved[:limit], resolved[:3]

def fmt_ts(ts_str):
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
def print_banner(clear=False):
    if clear and sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")
    print(BANNER)
    separator("─", C.BBLUE)

def print_session(sess, plan_limits):
    section_header("CURRENT SESSION  (5-hour rolling window)")

    total   = sess["total"]
    limit   = plan_limits["session"]
    start   = sess["session_start"]
    model   = sess["model"]
    scanned = sess["files_scanned"]

    now = datetime.now(timezone.utc)
    if start:
        elapsed_s   = int((now - start).total_seconds())
        el_h, rem   = divmod(elapsed_s, 3600)
        el_m        = rem // 60
        remaining_s = max(0, SESSION_WINDOW_HOURS * 3600 - elapsed_s)
        rem_h, rem2 = divmod(remaining_s, 3600)
        rem_m       = rem2 // 60
        elapsed_str   = f"{el_h}h {el_m:02d}m"
        remaining_str = f"{rem_h}h {rem_m:02d}m"
    else:
        elapsed_str = remaining_str = "N/A"

    print(f"\n  {C.DIM}Model:{C.RESET}    {C.BCYAN}{model}{C.RESET}   "
          f"{C.DIM}Files scanned:{C.RESET} {scanned}")
    print(f"  {C.DIM}Started:{C.RESET}  {start.strftime('%H:%M:%S UTC') if start else 'No active session'}"
          f"   {C.DIM}Elapsed:{C.RESET} {elapsed_str}   {C.DIM}Remaining:{C.RESET} {remaining_str}\n")

    rows = [
        ("Input tokens",      sess["input"]),
        ("Output tokens",     sess["output"]),
        ("Cache reads",       sess["cache_read"]),
        ("Cache writes",      sess["cache_write"]),
        ("Total tokens used", total),
    ]
    for label, val in rows:
        bold  = C.BOLD if label.startswith("Total") else ""
        color = C.BWHITE if label.startswith("Total") else C.WHITE
        print(f"  {bold}{color}{label:<22}{C.RESET}  {C.BYELLOW}{fmt_tokens(val):>8}{C.RESET}")

    if limit:
        pct = min(100.0, total / limit * 100)
        bar = progress_bar(pct)
        limit_color = C.BRED if pct >= 90 else (C.BYELLOW if pct >= 75 else C.BGREEN)
        print(f"\n  {C.DIM}Session limit ({fmt_tokens(limit)}):{C.RESET}")
        print(f"  {bar}  {limit_color}{C.BOLD}{pct:5.1f}%{C.RESET}  "
              f"{C.DIM}({fmt_tokens(total)} / {fmt_tokens(limit)}){C.RESET}")
    else:
        print(f"\n  {C.DIM}Session limit: not applicable for API/pay-per-use plans{C.RESET}")

def print_monthly(monthly, plan_limits):
    section_header("MONTHLY USAGE  (billing month, 1st → now)")

    total = monthly["total"]
    limit = plan_limits["monthly"]
    ms    = monthly["month_start"]

    print(f"\n  {C.DIM}Month from:{C.RESET}  {ms.strftime('%Y-%m-01 UTC')}\n")
    rows = [
        ("Input tokens",      monthly["input"]),
        ("Output tokens",     monthly["output"]),
        ("Total this month",  total),
    ]
    for label, val in rows:
        bold  = C.BOLD if label.startswith("Total") else ""
        color = C.BWHITE if label.startswith("Total") else C.WHITE
        print(f"  {bold}{color}{label:<22}{C.RESET}  {C.BYELLOW}{fmt_tokens(val):>8}{C.RESET}")

    if limit:
        pct = min(100.0, total / limit * 100)
        bar = progress_bar(pct)
        limit_color = C.BRED if pct >= 90 else (C.BYELLOW if pct >= 75 else C.BGREEN)
        print(f"\n  {C.DIM}Monthly limit (~{fmt_tokens(limit)}):{C.RESET}")
        print(f"  {bar}  {limit_color}{C.BOLD}{pct:5.1f}%{C.RESET}  "
              f"{C.DIM}({fmt_tokens(total)} / ~{fmt_tokens(limit)}){C.RESET}")
    else:
        print(f"\n  {C.DIM}Monthly limit: not applicable for API/pay-per-use plans{C.RESET}")

def print_service_status(components, overall_status):
    section_header("SERVICE STATUS")
    print()

    ov_ind  = overall_status.get("indicator", "none")
    ov_desc = overall_status.get("description", "")
    if ov_ind == "none":
        ov_str = f"{C.BGREEN}{C.BOLD}●  {ov_desc}{C.RESET}"
    elif ov_ind == "minor":
        ov_str = f"{C.BYELLOW}{C.BOLD}●  {ov_desc}{C.RESET}"
    else:
        ov_str = f"{C.BRED}{C.BOLD}●  {ov_desc}{C.RESET}"
    print(f"  Overall:    {ov_str}\n")

    for key in ["Claude API", "Claude Code", "claude.ai", "Console"]:
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
        for inc in resolved:
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

    jsonl_files, sl_path = find_jsonl_files()
    sess    = parse_session_tokens(jsonl_files)
    monthly = parse_monthly_tokens(jsonl_files)

    # Fill model from statusline when the JSONL records don't include it
    if sess["model"] == "unknown" and sl_path.exists():
        try:
            last = None
            with open(sl_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.strip():
                        try:
                            last = json.loads(line)
                        except json.JSONDecodeError:
                            pass
            if last:
                sess["model"] = last.get("model", "unknown")
        except (OSError, PermissionError):
            pass

    status_data  = fetch_status()
    components   = {}
    overall      = {"indicator": "none", "description": "All Systems Operational"}
    unresolved   = []
    resolved_inc = []

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

    print_banner(clear=auto_refresh is not None)
    print_session(sess, plan_limits)
    print_monthly(monthly, plan_limits)
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
        epilog=f"""
Plans:
  pro     Claude Pro         (~44K tokens / 5-hr session,  ~1M tokens / month)
  max5    Claude Max 5×      (~220K tokens / 5-hr session, ~5M tokens / month)
  max20   Claude Max 20×     (~880K tokens / 5-hr session, ~20M tokens / month)
  api     API / pay-per-use  (no hard token limits shown)

Notes:
  Monthly limits are estimates — Anthropic defines Max 5× and Max 20× as
  "5× / 20× more usage than Pro" without publishing exact token values.
  Limits reset on your monthly billing cycle (approximated here as the 1st).
  The 5-hour window is a rolling lookback, not anchored to your
  actual session start time.

Examples:
  python claude_usage_monitor.py
  python claude_usage_monitor.py --plan max5
  python claude_usage_monitor.py --watch 30
  python claude_usage_monitor.py --plan max20 --watch 60
  python claude_usage_monitor.py --no-color | grep Total

Version: {__version__}
        """,
    )
    parser.add_argument(
        "--plan", choices=["pro", "max5", "max20", "api"],
        default="pro", help="Your Claude subscription plan (default: pro)"
    )
    parser.add_argument(
        "--watch", type=int, metavar="SECONDS",
        help="Auto-refresh interval in seconds, minimum 5 (e.g. --watch 30)"
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Strip ANSI colour codes (auto-detected when output is not a TTY)"
    )
    args = parser.parse_args()

    if args.no_color or not sys.stdout.isatty():
        import builtins
        _real_print = builtins.print
        builtins.print = lambda *a, **k: _real_print(
            *(re.sub(r'\033\[[0-9;]*m', '', str(x)) for x in a), **k
        )
        global BANNER
        BANNER = re.sub(r'\033\[[0-9;]*m', '', BANNER)

    if args.watch is not None:
        if args.watch < 5:
            parser.error("--watch interval must be at least 5 seconds")
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
