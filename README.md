# Claude Usage Monitor

Terminal dashboard for Claude Code — tracks token consumption and live service status.

```
             C O D E      U S A G E   &   S T A T U S
         Reads ~/.claude JSONL logs + status.anthropic.com
```

## What it does

- Parses `~/.claude/projects/*/` JSONL session logs to compute token usage over three windows:
  - **5-hour rolling window** — mirrors Claude's rate-limit cycle
  - **Current calendar week** (Mon–Sun)
  - **Current billing month** (1st of month → now)
- Fetches `status.anthropic.com` for live service health and active incidents
- Renders a colour-coded terminal dashboard with progress bars that warn at 75% and go critical at 90%

## Requirements

Python 3.9+. No dependencies — stdlib only.

## Usage

```bash
# One-shot snapshot (defaults to Pro plan limits)
python claude_usage_monitor.py

# Specify your plan
python claude_usage_monitor.py --plan max5

# Auto-refresh every 30 seconds (minimum interval: 5s)
python claude_usage_monitor.py --plan max20 --watch 30

# Plain text output — safe to pipe or redirect
python claude_usage_monitor.py --no-color | grep Total
```

## Plan limits

All values are estimates. Anthropic defines Max 5× and Max 20× as "5× / 20× more usage than Pro" without publishing exact token counts. Monthly limits reset on your billing cycle (approximated here as the 1st of the month).

| Flag    | Plan              | Session (5 hr) | Weekly    | Monthly  |
|---------|-------------------|----------------|-----------|----------|
| `pro`   | Claude Pro        | ~500K tokens   | ~2.4M     | ~10M     |
| `max5`  | Claude Max 5×     | ~2.5M tokens   | ~12M      | ~50M     |
| `max20` | Claude Max 20×    | ~10M tokens    | ~48M      | ~200M    |
| `api`   | API / pay-per-use | —              | —         | —        |

## Data sources

| Section         | Source                                      |
|-----------------|---------------------------------------------|
| Token usage     | `~/.claude/projects/*/*.jsonl` (local only) |
| Service status  | `status.anthropic.com/api/v2/summary.json`  |
| Active model    | JSONL logs → `~/.claude/statusline.jsonl`   |

No data leaves your machine except the read-only status API call.

## Caveats

- **Token counts are input + output only.** Cache reads and cache writes are shown separately but excluded from the totals used in progress bars.
- **The 5-hour window is a rolling lookback**, not anchored to your actual session start. The "remaining" time shown is approximate.
- **Plan token limits are hardcoded estimates** and may drift if Anthropic changes them.
