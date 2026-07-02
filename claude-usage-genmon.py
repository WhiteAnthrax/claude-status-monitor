#!/usr/bin/env python3
"""Claude 利用状況を XFCE4 GenMon プラグイン向けに出力する。

第一候補: Claude の OAuth ログイン情報を使って公式の利用状況エンドポイント
(/api/oauth/usage) に問い合わせ、5時間枠・週次の「残り%」とリセット時刻を表示する。
これは /usage コマンドと同じデータで、レートリミット状況の問い合わせのみ
（プロンプト送信ではない）なので Claude クレジット（利用枠）は消費しない。

アクセストークンが期限切れの場合は、refresh_token でトークン更新を試みる
（これも推論ではないのでクレジット消費なし）。連打で更新エンドポイントを叩き
すぎないよう、以下の安全弁を設ける:
  - 更新はトークンが実際に期限切れの時だけ行う
  - 最短 MIN_REFRESH_INTERVAL 秒に1回まで（下限）＋失敗時は指数バックオフ
  - Claude Code 本体と同じ .credentials.json.lock ディレクトリロックで直列化
    （proper-lockfile 互換）。ロックが取れなければ今回はスキップ。
  - 更新に成功したトークンはアトミックに書き戻す（他フィールド保持・mode 0600）

フォールバック: 認証切れ・オフライン・エラー時は、ローカルログ
(~/.claude/projects/**/*.jsonl) からトークン消費量を集計して表示する。
こちらは完全ローカル・ネット非接続。

依存: Python3 標準ライブラリのみ。
"""

import glob
import json
import os
import tempfile
import time
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
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code の公開 OAuth client_id

HTTP_TIMEOUT = 4          # usage 取得のタイムアウト（パネルを固まらせない）
REFRESH_TIMEOUT = 8       # トークン更新のタイムアウト
BLOCK_HOURS = 5

# --- 安全弁 ---------------------------------------------------------------
MIN_REFRESH_INTERVAL = 300    # 更新試行の下限間隔（秒）。これ未満の連打では更新しない
MAX_REFRESH_BACKOFF = 3600    # 連続失敗時のバックオフ上限（秒）
USAGE_CACHE_TTL = 15          # usage 取得結果のキャッシュ秒数（クリック連打の debounce）
LOCK_STALE = 60               # ロックがこの秒数より古ければ残骸とみなす（安全に破棄）
LOCK_RETRIES = 10             # ロック取得のリトライ回数（×0.2秒）

STATE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "claude-status-monitor",
)
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOCK_DIR = CREDENTIALS + ".lock"   # proper-lockfile 既定（<file>.lock）に相乗り


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
# 状態ファイル（安全弁の記録）
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    """状態を保存。成功なら True。保存できないなら False（＝更新は見送る判断に使う）。"""
    tmp = None
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".state", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_FILE)
        return True
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


# ---------------------------------------------------------------------------
# 認証情報
# ---------------------------------------------------------------------------
def load_oauth():
    try:
        with open(CREDENTIALS, "r", encoding="utf-8") as fh:
            return json.load(fh).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return {}


def is_expired(oauth, now_ms):
    try:
        return float(oauth.get("expiresAt", 0)) <= now_ms
    except (TypeError, ValueError):
        return True


# ---------------------------------------------------------------------------
# ロック（proper-lockfile 互換の mkdir ディレクトリロック）
# ---------------------------------------------------------------------------
def acquire_lock():
    for _ in range(LOCK_RETRIES):
        try:
            os.mkdir(LOCK_DIR)
            return True
        except FileExistsError:
            # 残骸ロックのみ安全に破棄（活きているロックは mtime が更新され続ける）
            try:
                if time.time() - os.stat(LOCK_DIR).st_mtime > LOCK_STALE:
                    os.rmdir(LOCK_DIR)
                    continue
            except OSError:
                pass
            time.sleep(0.2)
        except OSError:
            return False
    return False


def release_lock():
    try:
        os.rmdir(LOCK_DIR)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# トークン更新（安全弁つき）
# ---------------------------------------------------------------------------
def write_credentials(access_token, refresh_token, expires_at_ms):
    """全フィールドを保持したままトークンを更新し、アトミックに書き戻す（mode 0600）。"""
    with open(CREDENTIALS, "r", encoding="utf-8") as fh:
        full = json.load(fh)
    oauth = full.setdefault("claudeAiOauth", {})
    oauth["accessToken"] = access_token
    if refresh_token:
        oauth["refreshToken"] = refresh_token
    oauth["expiresAt"] = int(expires_at_ms)

    dir_ = os.path.dirname(CREDENTIALS) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".cred", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(full, fh)
        os.replace(tmp, CREDENTIALS)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def do_refresh(refresh_token, now_ms):
    """refresh_token で更新エンドポイントを叩き、成功なら書き戻して True。"""
    body = json.dumps(
        {"grant_type": "refresh_token", "refresh_token": refresh_token,
         "client_id": CLIENT_ID}
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "claude-status-monitor"},
    )
    try:
        with urllib.request.urlopen(req, timeout=REFRESH_TIMEOUT) as resp:
            data = json.load(resp)
    except (OSError, ValueError, urllib.error.URLError):
        return False
    access = data.get("access_token")
    expires_in = data.get("expires_in")
    if not access or not expires_in:
        return False
    try:
        write_credentials(access, data.get("refresh_token"),
                          now_ms + int(expires_in) * 1000)
    except (OSError, ValueError):
        return False
    return True


def maybe_refresh(now):
    """安全弁を通した上で、期限切れトークンの更新を試みる。"""
    now_ms = now * 1000
    state = load_state()
    last = state.get("last_refresh_attempt", 0)
    fails = state.get("refresh_fail_count", 0)

    # 安全弁: 下限間隔＋指数バックオフ。これ未満の間隔では叩かない。
    wait = MIN_REFRESH_INTERVAL
    if fails:
        wait = min(MIN_REFRESH_INTERVAL * (2 ** fails), MAX_REFRESH_BACKOFF)
    if now - last < wait:
        return

    if not acquire_lock():
        return
    try:
        # ロック下で再確認: 別プロセスが更新済みなら何もしない
        oauth = load_oauth()
        if not is_expired(oauth, now_ms):
            return
        refresh_token = oauth.get("refreshToken")
        if not refresh_token:
            return
        # 試行を先に記録（保存できないなら連打防止のため中止）
        state["last_refresh_attempt"] = now
        if not save_state(state):
            return
        ok = do_refresh(refresh_token, now_ms)
        state = load_state()
        state["last_refresh_attempt"] = now
        state["refresh_fail_count"] = 0 if ok else fails + 1
        save_state(state)
    finally:
        release_lock()


# ---------------------------------------------------------------------------
# 公式エンドポイント
# ---------------------------------------------------------------------------
def http_get_usage(token):
    req = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": f"Bearer {token}",
                 "anthropic-beta": "oauth-2025-04-20",
                 "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json",
                 "User-Agent": "claude-status-monitor"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.load(resp)
    except (OSError, ValueError, urllib.error.URLError):
        return None


def render_official(data):
    def window(obj):
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
        return f"{dt.astimezone():%m/%d %H:%M} (残り {human_duration(dt - now)})" if dt else "?"

    parts = []
    if fh_rem is not None:
        parts.append(f"5h:{fh_rem}%")
    if wk_rem is not None:
        parts.append(f"7d:{wk_rem}%")
    lowest = min([r for r in (fh_rem, wk_rem) if r is not None], default=100)
    txt = ("⚠ " if lowest <= 15 else "") + "残 " + " ".join(parts)

    lines = ["Claude 残量（公式 /api/oauth/usage・クレジット消費なし）"]
    if fh_rem is not None:
        lines.append(f"5時間枠: 残り {fh_rem}% (使用 {100 - fh_rem}%)  リセット {reset_str(fh_reset)}")
    if wk_rem is not None:
        lines.append(f"週次(7日): 残り {wk_rem}% (使用 {100 - wk_rem}%)  リセット {reset_str(wk_reset)}")
    lines.append(f"取得時刻: {datetime.now():%H:%M:%S}")
    return txt, "\n".join(lines)


def fetch_official():
    now = time.time()
    now_ms = now * 1000

    # debounce: 直近の成功結果があれば使い回す（クリック連打対策の安全弁）
    state = load_state()
    cache = state.get("usage_cache")
    if cache and now - cache.get("ts", 0) < USAGE_CACHE_TTL:
        return cache["txt"], cache["tool"]

    oauth = load_oauth()
    if not oauth.get("accessToken"):
        return None
    if is_expired(oauth, now_ms):
        maybe_refresh(now)          # 安全弁つき更新
        oauth = load_oauth()        # 更新後を読み直す
    if is_expired(oauth, now_ms) or not oauth.get("accessToken"):
        return None                 # まだ切れている → フォールバックへ

    data = http_get_usage(oauth["accessToken"])
    if data is None:
        return None
    rendered = render_official(data)
    if rendered is None:
        return None

    txt, tool = rendered
    state = load_state()
    state["usage_cache"] = {"ts": now, "txt": txt, "tool": tool}
    save_state(state)
    return txt, tool


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
    try:
        result = fetch_official()
    except Exception:
        result = None
    if result is None:
        result = local_fallback()
    txt, tool = result
    print(txt)
    print(f"<txt>{txt}</txt>")
    print(f"<tool>{tool}</tool>")


if __name__ == "__main__":
    main()
