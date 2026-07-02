#!/usr/bin/env bash
#
# claude-status-monitor の GenMon プラグインをパネルから取り外す。
#
set -euo pipefail
CHANNEL="xfce4-panel"

# 本ツールの GenMon（command に集計スクリプト名を含む）を探す
PLUGIN_ID=""
while read -r prop val; do
  case "$prop" in
    /plugins/plugin-*/command)
      if [[ "$val" == *"claude-usage-genmon.py"* ]]; then
        PLUGIN_ID="$(echo "$prop" | grep -oE 'plugin-[0-9]+' | grep -oE '[0-9]+')"
      fi ;;
  esac
done < <(xfconf-query -c "${CHANNEL}" -lv 2>/dev/null)

[ -n "${PLUGIN_ID}" ] || { echo "本ツールの GenMon は見つかりませんでした。何もしません。"; exit 0; }
echo "取り外す対象: plugin-${PLUGIN_ID}"

# panel の plugin-ids から除外
PANEL_NUM=$(xfconf-query -c "${CHANNEL}" -p /panels 2>/dev/null | grep -E '^[0-9]+$' | head -1)
PANEL_PATH="/panels/panel-${PANEL_NUM:-1}"
mapfile -t IDS < <(xfconf-query -c "${CHANNEL}" -p "${PANEL_PATH}/plugin-ids" 2>/dev/null | grep -E '^[0-9]+$')
ARGS=()
for id in "${IDS[@]}"; do [ "$id" = "${PLUGIN_ID}" ] || ARGS+=(-t int -s "$id"); done
[ ${#ARGS[@]} -gt 0 ] && xfconf-query -c "${CHANNEL}" -p "${PANEL_PATH}/plugin-ids" -n "${ARGS[@]}"

# プラグイン設定を削除
xfconf-query -c "${CHANNEL}" -p "/plugins/plugin-${PLUGIN_ID}" -rR 2>/dev/null || true

command -v xfce4-panel >/dev/null && { xfce4-panel -r >/dev/null 2>&1 || true; }
echo "取り外しました。"
