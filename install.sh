#!/usr/bin/env bash
#
# claude-status-monitor を XFCE4 パネルの GenMon プラグインとして設定する。
#
# 冪等（idempotent）かつ自己修復的:
#  - 本ツールの GenMon が既にあれば再利用、無ければ新規追加。
#  - 毎回プラグイン型(genmon)とコマンド等をクリーンに設定し直すので、
#    過去の失敗で「(null) プラグインをロードできませんでした」状態でも直る。
#  - plugin-ids に残った壊れた(型なし)エントリも掃除する。
# スクリプト自身の場所を自動検出するので、リポジトリをどこに置いても動く。
#
# 使い方:  ./install.sh [更新間隔(秒)]      # 既定 300 秒(5分)
#
set -euo pipefail

CHANNEL="xfce4-panel"
PERIOD_SEC="${1:-300}"
PERIOD_MS=$(( PERIOD_SEC * 1000 ))

# --- 集計スクリプト/インタプリタの絶対パスを解決 ---
# genmon はパネルのプロセス環境(PATHが最小限のことがある)でコマンドを実行するため、
# python3 はフルパスで指定する（PATH に依存せず確実に起動させる）。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
MONITOR_PY="${SCRIPT_DIR}/claude-usage-genmon.py"
PYTHON_BIN="$(command -v python3 || true)"
COMMAND="${PYTHON_BIN:-python3} ${MONITOR_PY}"

# --- 前提チェック ---
command -v xfconf-query >/dev/null || { echo "エラー: xfconf-query が見つかりません（XFCE 環境で実行してください）"; exit 1; }
command -v python3      >/dev/null || { echo "エラー: python3 が見つかりません"; exit 1; }
[ -f "${MONITOR_PY}" ]             || { echo "エラー: ${MONITOR_PY} が見つかりません"; exit 1; }

# GenMon プラグイン本体(libgenmon.so)が無いと「(null)」になるのでハードチェック。
GENMON_LIB="$(find /usr/lib /usr/lib64 /usr/local/lib -maxdepth 4 -name libgenmon.so 2>/dev/null | head -1)"
if [ -z "${GENMON_LIB}" ]; then
  echo "エラー: GenMon プラグイン本体 (libgenmon.so) が見つかりません。"
  echo "       この環境には xfce4-genmon-plugin が未インストールです。先に導入してください:"
  echo "         Arch     : sudo pacman -S xfce4-genmon-plugin"
  echo "         Debian/Ubuntu: sudo apt install xfce4-genmon-plugin"
  echo "         Fedora   : sudo dnf install xfce4-genmon-plugin"
  echo "         openSUSE : sudo zypper install xfce4-genmon-plugin"
  exit 1
fi
echo "GenMon 本体    : ${GENMON_LIB}"
echo "集計スクリプト : ${MONITOR_PY}"
echo "更新間隔       : ${PERIOD_SEC} 秒 (${PERIOD_MS} ms)"

# --- 実行中のパネルと同じセッション(DBUS/DISPLAY)に合わせる ---
# SSH(ssh -X)やリモートデスクトップだと、シェルの DISPLAY/DBUS が画面上のパネルと
# ずれ、xfconf 変更や -r が別セッションに届いて反映されないことがある。
# 実際に動いている xfce4-panel の環境を借りて、確実に同じセッションを操作する。
PANEL_PID="$(pgrep -u "$(id -u)" -x xfce4-panel 2>/dev/null | head -1 || true)"
if [ -n "${PANEL_PID}" ] && [ -r "/proc/${PANEL_PID}/environ" ]; then
  penv() { tr '\0' '\n' < "/proc/${PANEL_PID}/environ" 2>/dev/null | grep -m1 "^$1=" | cut -d= -f2- ; }
  p_dbus="$(penv DBUS_SESSION_BUS_ADDRESS)"; p_disp="$(penv DISPLAY)"
  [ -n "${p_dbus}" ] && export DBUS_SESSION_BUS_ADDRESS="${p_dbus}"
  [ -n "${p_disp}" ] && export DISPLAY="${p_disp}"
  echo "パネルセッション: PID ${PANEL_PID} に整合 (DISPLAY=${DISPLAY:-?})"
else
  echo "警告: 動作中の xfce4-panel が見つかりません（このセッションに反映されない可能性）。"
fi

# --- 対象パネルを検出（/panels 配列の先頭。無ければ 1） ---
PANEL_NUM="$(xfconf-query -c "${CHANNEL}" -p /panels 2>/dev/null | grep -E '^[0-9]+$' | head -1 || true)"
PANEL_NUM="${PANEL_NUM:-1}"
PANEL_PATH="/panels/panel-${PANEL_NUM}"
echo "対象パネル     : ${PANEL_PATH}"

# 指定 plugin-id の型文字列を返す（未設定なら空）
plugin_type() { xfconf-query -c "${CHANNEL}" -p "/plugins/plugin-$1" 2>/dev/null || true; }

# --- 既存の本ツール GenMon を探す（command に集計スクリプト名を含むもの） ---
PLUGIN_ID=""
while read -r prop val; do
  case "$prop" in
    /plugins/plugin-*/command)
      [[ "$val" == *"claude-usage-genmon.py"* ]] && \
        PLUGIN_ID="$(printf '%s' "$prop" | grep -oE 'plugin-[0-9]+' | grep -oE '[0-9]+')" ;;
  esac
done < <(xfconf-query -c "${CHANNEL}" -lv 2>/dev/null)

if [ -n "${PLUGIN_ID}" ]; then
  echo "既存の GenMon を再利用: plugin-${PLUGIN_ID}"
else
  MAX_ID="$(xfconf-query -c "${CHANNEL}" -l 2>/dev/null | grep -oE '/plugins/plugin-[0-9]+' | grep -oE '[0-9]+$' | sort -n | tail -1 || true)"
  PLUGIN_ID=$(( ${MAX_ID:-0} + 1 ))
  echo "新規 GenMon を作成: plugin-${PLUGIN_ID}"
fi

# --- プラグインをクリーンに(再)構築: 型を必ず設定し直すのが肝 ---
xfconf-query -c "${CHANNEL}" -p "/plugins/plugin-${PLUGIN_ID}" -rR 2>/dev/null || true
xfconf-query -c "${CHANNEL}" -p "/plugins/plugin-${PLUGIN_ID}"               -n -t string -s genmon
xfconf-query -c "${CHANNEL}" -p "/plugins/plugin-${PLUGIN_ID}/command"       -n -t string -s "${COMMAND}"
xfconf-query -c "${CHANNEL}" -p "/plugins/plugin-${PLUGIN_ID}/update-period" -n -t int    -s "${PERIOD_MS}"
xfconf-query -c "${CHANNEL}" -p "/plugins/plugin-${PLUGIN_ID}/use-label"     -n -t bool   -s false

# --- 型が確定したか検証（ここが空だと「(null)」になる） ---
if [ "$(plugin_type "${PLUGIN_ID}")" != "genmon" ]; then
  echo "エラー: plugin-${PLUGIN_ID} の型を genmon に設定できませんでした。"
  echo "        xfconf-query の権限/セッション（DISPLAY, DBUS_SESSION_BUS_ADDRESS）を確認してください。"
  exit 1
fi

# --- plugin-ids を再構築: 壊れた(型なし)エントリを除去しつつ、自分を含める ---
mapfile -t OLD_IDS < <(xfconf-query -c "${CHANNEL}" -p "${PANEL_PATH}/plugin-ids" 2>/dev/null | grep -E '^[0-9]+$' || true)
NEW_IDS=(); pruned=""
for id in "${OLD_IDS[@]}"; do
  if [ "$id" = "${PLUGIN_ID}" ]; then
    continue                              # 自分は後で末尾に追加
  elif [ -z "$(plugin_type "$id")" ]; then
    pruned="${pruned} ${id}"              # 型なし=壊れたエントリ → 掃除
  else
    NEW_IDS+=("$id")
  fi
done
NEW_IDS+=("${PLUGIN_ID}")                  # 自分を末尾(時計付近)へ

ARGS=(); for id in "${NEW_IDS[@]}"; do ARGS+=(-t int -s "$id"); done
xfconf-query -c "${CHANNEL}" -p "${PANEL_PATH}/plugin-ids" -n "${ARGS[@]}"
echo "plugin-ids     : ${NEW_IDS[*]}"
[ -n "${pruned}" ] && echo "掃除した壊れたID:${pruned}"

# --- パネル再読み込み ---
if command -v xfce4-panel >/dev/null; then
  xfce4-panel -r >/dev/null 2>&1 || true
  echo "パネルを再読み込みしました。"
fi

echo
echo "完了。パネル（時計付近）に残量が表示されます。アイコンをクリックすると即時更新。"
echo "取り外すには ./uninstall.sh を実行してください。"
