#!/usr/bin/env python3
"""Claude 利用状況を XFCE4 GenMon プラグイン向けに出力する。

第一候補: Claude の OAuth ログイン情報を使って公式の利用状況エンドポイント
(/api/oauth/usage) に問い合わせ、5時間枠・週次の「残り%」とリセット時刻を表示する。
これは /usage コマンドと同じデータで、レートリミット状況の問い合わせのみ
（プロンプト送信ではない）なので Claude クレジット（利用枠）は消費しない。

フォールバック: 認証切れ・オフライン・エラー時は、ローカルログ
(~/.claude/projects/**/*.jsonl) からトークン消費量を集計して表示する。
こちらは完全ローカル・ネット非接続。

依存: Python3 標準ライブラリのみ。
"""

import glob
import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

CREDENTIALS = os.environ.get(
    "CLAUDE_CREDENTIALS", os.path.expanduser("~/.claude/.credentials.json")
)
PROJECTS_DIR = os.environ.get(
    "CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects")
)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
HTTP_TIMEOUT = 4  # 秒。パネルが固まらないよう短めに。
BLOCK_HOURS = 5


# ---------------------------------------------------------------------------
# 表示ヘルパ
# ---------------------------------------------------------------------------
def human(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(int(n))


def human_duration(td):
    total = max(0, int(td.total_seconds()))
    h, rem = divmod(total, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


def parse_ts(raw):
    """ISO8601(...Z / +00:00) を aware datetime(UTC) に。失敗時 None。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 公式エンドポイント（第一候補）
# ---------------------------------------------------------------------------
def fetch_official():
    """/api/oauth/usage を叩いて (txt, tool) を返す。失敗時は None。"""
    try:
        with open(CREDENTIALS, "r", encoding="utf-8") as fh:
            cred = json.load(fh).get("claudeAiOauth") or {}
        token = cred.get("accessToken")
        if not token:
            return None
        req = urllib.request.Request(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                "User-Agent": "claude-status-monitor",
            },
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.load(resp)
    except (OSError, ValueError, urllib.error.URLError):
        return None

    def window(obj):
        """{'utilization':35.0,'resets_at':...} -> (remaining%, reset_dt)"""
        if not isinstance(obj, dict):
            return None, None
        util = obj.get("utilization")
        if util is None:
            return None, None
        return max(0, round(100 - float(util))), parse_ts(obj.get("resets_at"))

    fh_rem, fh_reset = window(data.get("five_hour"))
    wk_rem, wk_reset = window(data.get("seven_day"))
    if fh_rem is None and wk_rem is None:
        return None

    now = datetime.now(timezone.utc)

    def reset_str(dt):
        if not dt:
            return "?"
        return f"{dt.astimezone():%m/%d %H:%M} (残り {human_duration(dt - now)})"

    # パネル本体: 残り%（少ない方に警告マーク）
    parts = []
    if fh_rem is not None:
        parts.append(f"5h:{fh_rem}%")
    if wk_rem is not None:
        parts.append(f"7d:{wk_rem}%")
    lowest = min([r for r in (fh_rem, wk_rem) if r is not None], default=100)
    mark = "⚠ " if lowest <= 15 else ""
    txt = f"{mark}残 " + " ".join(parts)

    tool_lines = ["Claude 残量（公式 /api/oauth/usage・クレジット消費なし）"]
    if fh_rem is not None:
        tool_lines.append(
            f"5時間枠: 残り {fh_rem}% (使用 {100 - fh_rem}%)  リセット {reset_str(fh_reset)}"
        )
    if wk_rem is not None:
        tool_lines.append(
            f"週次(7日): 残り {wk_rem}% (使用 {100 - wk_rem}%)  リセット {reset_str(wk_reset)}"
        )
    tool_lines.append(f"取得時刻: {datetime.now():%H:%M:%S}")
    return txt, "\n".join(tool_lines)


# ---------------------------------------------------------------------------
# ローカルログ集計（フォールバック）
# ---------------------------------------------------------------------------
def iter_usage_events():
    for path in glob.iglob(os.path.join(PROJECTS_DIR, "**", "*.jsonl"), recursive=True):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") != "assistant":
                        continue
                    usage = (rec.get("message") or {}).get("usage")
                    ts = parse_ts(rec.get("timestamp"))
                    if not isinstance(usage, dict) or ts is None:
                        continue
                    tokens = (
                        (usage.get("input_tokens") or 0)
                        + (usage.get("output_tokens") or 0)
                        + (usage.get("cache_creation_input_tokens") or 0)
                        + (usage.get("cache_read_input_tokens") or 0)
                    )
                    yield ts, int(tokens)
        except OSError:
            continue


def active_block(events):
    if not events:
        return None
    block_start = events[0][0].replace(minute=0, second=0, microsecond=0)
    block_tokens = 0
    prev = events[0][0]
    for ts, tok in events:
        if ts - prev > timedelta(hours=BLOCK_HOURS):
            block_start = ts.replace(minute=0, second=0, microsecond=0)
            block_tokens = 0
        block_tokens += tok
        prev = ts
    return block_start, block_tokens


def local_fallback():
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now().astimezone()
    events = sorted(iter_usage_events(), key=lambda e: e[0])
    if not events:
        return "Claude –", ("Claude 利用状況: ログが見つかりません\n"
                            f"探索先: {PROJECTS_DIR}")

    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today = sum(t for ts, t in events if ts >= midnight.astimezone(timezone.utc))
    week = sum(t for ts, t in events if ts >= now_utc - timedelta(days=7))
    block_start, block_tokens = active_block(events)
    block_end = block_start + timedelta(hours=BLOCK_HOURS)

    if now_utc < block_end:
        rem = human_duration(block_end - now_utc)
        txt = f"~5h {human(block_tokens)} ⟳{rem}"
        block_line = f"5時間枠: {human(block_tokens)} tok (リセット {block_end.astimezone():%H:%M} / 残り {rem})"
    else:
        txt = f"~今日 {human(today)}"
        block_line = "5時間枠: アイドル"

    tool = (
        "Claude 利用状況（公式APIに接続できずローカルログで代替・消費量表示）\n"
        f"{block_line}\n"
        f"今日: {human(today)} tok\n"
        f"今週(7日): {human(week)} tok\n"
        f"最終ログ更新: {events[-1][0].astimezone():%m/%d %H:%M:%S}"
    )
    return txt, tool


# ---------------------------------------------------------------------------
def main():
    result = fetch_official() or local_fallback()
    txt, tool = result
    print(txt)
    print(f"<txt>{txt}</txt>")
    print(f"<tool>{tool}</tool>")


if __name__ == "__main__":
    main()
