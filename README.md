# Claude Usage Monitor

Terminal dashboard for Claude Code — tracks token consumption and live service status.

```
             C O D E      U S A G E   &   S T A T U S
         Reads ~/.claude JSONL logs + status.anthropic.com
```

## What it does

- Parses `~/.claude/projects/*/` JSONL session logs to compute token usage over two windows:
  - **5-hour rolling window** — mirrors Claude's rate-limit cycle
  - **Current calendar week** (Mon–Sun)
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

# Auto-refresh every 30 seconds
python claude_usage_monitor.py --plan max20 --watch 30
```

## Plan limits

| Flag    | Plan             | Session limit | Weekly limit |
|---------|------------------|--------------|--------------|
| `pro`   | Claude Pro       | 44K tokens   | 308K tokens  |
| `max5`  | Claude Max 5×    | 88K tokens   | 616K tokens  |
| `max20` | Claude Max 20×   | 220K tokens  | 1.54M tokens |
| `api`   | API / pay-per-use | —           | —            |

## Data sources

| Section         | Source                                      |
|-----------------|---------------------------------------------|
| Token usage     | `~/.claude/projects/*/*.jsonl` (local only) |
| Service status  | `status.anthropic.com/api/v2/summary.json`  |
| Active model    | JSONL logs → `~/.claude/statusline.jsonl`   |

No data leaves your machine except the read-only status API call.
