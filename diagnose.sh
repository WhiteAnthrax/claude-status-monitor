#!/usr/bin/env bash
#
# GenMon にパネル表示が出ない時の切り分け診断。
# machine B のターミナルで  ./diagnose.sh  と実行し、出力を共有してください。
# （読み取り中心。install.sh の再適用も行います）
#
CHANNEL="xfce4-panel"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
MONITOR_PY="${SCRIPT_DIR}/claude-usage-genmon.py"

# 画面表示と同時に diagnose-out.txt にも保存（コピペできない環境向け）。
OUT="${SCRIPT_DIR}/diagnose-out.txt"
exec > >(tee "${OUT}") 2>&1
echo "(この出力は ${OUT} にも保存されます)"

# 実行中のパネルと同じセッション(DBUS/DISPLAY)に合わせる（SSH/リモート対策）。
SHELL_DISPLAY="${DISPLAY:-}"; SHELL_DBUS="${DBUS_SESSION_BUS_ADDRESS:-}"
PANEL_PID="$(pgrep -u "$(id -u)" -x xfce4-panel 2>/dev/null | head -1 || true)"
PANEL_DISPLAY=""; PANEL_DBUS=""
if [ -n "${PANEL_PID}" ] && [ -r "/proc/${PANEL_PID}/environ" ]; then
  penv() { tr '\0' '\n' < "/proc/${PANEL_PID}/environ" 2>/dev/null | grep -m1 "^$1=" | cut -d= -f2- ; }
  PANEL_DBUS="$(penv DBUS_SESSION_BUS_ADDRESS)"; PANEL_DISPLAY="$(penv DISPLAY)"
  [ -n "${PANEL_DBUS}" ] && export DBUS_SESSION_BUS_ADDRESS="${PANEL_DBUS}"
  [ -n "${PANEL_DISPLAY}" ] && export DISPLAY="${PANEL_DISPLAY}"
fi

echo "==================================================================="
echo " claude-status-monitor 診断"
echo " host      : $(hostname)"
echo " script_dir: ${SCRIPT_DIR}"
echo " DISPLAY   : ${DISPLAY:-(未設定)}"
echo "==================================================================="

echo
echo "### 1. python3 と、集計スクリプトの直接実行 ###"
echo "which python3 : $(command -v python3 || echo '見つからない')"
python3 --version 2>&1
echo "--- 実行結果 (ここに '残 5h:.. 7d:..' が出れば script はOK) ---"
python3 "${MONITOR_PY}"
echo "[exit=$?]"

echo
echo "### 2. install.sh は フルパス版(python3を絶対パス指定)か ###"
grep -n 'COMMAND=' "${SCRIPT_DIR}/install.sh" 2>/dev/null || echo "install.sh が読めません"

echo
echo "### 3. GenMon 本体 (libgenmon.so) の有無 ###"
find /usr/lib /usr/lib64 /usr/local/lib -maxdepth 4 -name libgenmon.so 2>/dev/null || echo "見つからない"
echo "パッケージ: $(pacman -Q xfce4-genmon-plugin 2>/dev/null || dpkg -l 2>/dev/null | grep -i genmon || rpm -q xfce4-genmon-plugin 2>/dev/null || echo '不明')"

echo
echo "### 4. install.sh を再適用 ###"
if [ -x "${SCRIPT_DIR}/install.sh" ]; then
  "${SCRIPT_DIR}/install.sh"
else
  echo "install.sh が実行できません（chmod +x install.sh を試してください）"
fi

echo
echo "### 5. 本ツールの genmon プラグイン設定（再読込後に生存しているか） ###"
found=""
while read -r prop val; do
  case "$prop" in
    /plugins/plugin-*/command)
      if [[ "$val" == *"claude-usage-genmon.py"* ]]; then
        pid="$(printf '%s' "$prop" | grep -oE 'plugin-[0-9]+' | grep -oE '[0-9]+')"
        found="$pid"
      fi ;;
  esac
done < <(xfconf-query -c "${CHANNEL}" -lv 2>/dev/null)
if [ -n "$found" ]; then
  echo "本ツールのプラグイン: plugin-${found}"
  xfconf-query -c "${CHANNEL}" -lv 2>/dev/null | grep "plugin-${found}\b"
  echo "型(genmonであるべき): '$(xfconf-query -c "${CHANNEL}" -p "/plugins/plugin-${found}" 2>/dev/null)'"
else
  echo "!! 本ツールの genmon プラグインが xfconf に見つかりません（クロバーで消えた可能性）"
fi

echo
echo "### 6. パネルの plugin-ids（上のIDが含まれているか） ###"
for pnum in $(xfconf-query -c "${CHANNEL}" -p /panels 2>/dev/null | grep -E '^[0-9]+$'); do
  ids="$(xfconf-query -c "${CHANNEL}" -p "/panels/panel-${pnum}/plugin-ids" 2>/dev/null | grep -E '^[0-9]+$' | tr '\n' ' ')"
  echo "panel-${pnum}: ${ids}"
done

echo
echo "### 7. パネルプロセスとセッション整合 ###"
echo "xfce4-panel プロセス数: $(pgrep -c xfce4-panel 2>/dev/null || echo 0) / PID=${PANEL_PID:-なし}"
echo "シェルの DISPLAY   : ${SHELL_DISPLAY:-(空)}     パネルの DISPLAY   : ${PANEL_DISPLAY:-(不明)}"
echo "シェルの DBUS      : ${SHELL_DBUS:-(空)}"
echo "パネルの DBUS      : ${PANEL_DBUS:-(不明)}"
if [ -n "${PANEL_DBUS}" ] && [ "${SHELL_DBUS}" != "${PANEL_DBUS}" ]; then
  echo ">> DBUS が食い違っていました（これが未表示の原因）。本スクリプトはパネル側に整合済みです。"
fi
ls -la ~/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml 2>/dev/null

echo
echo "==================================================================="
echo " 見かた:"
echo "  ・1 で残量が出て、5 に plugin-XX(genmon) があり、6 の plugin-ids に"
echo "    その番号が含まれていれば、設定は正常 → パネル上の一番端(時計付近)を確認。"
echo "  ・5/6 で消えている → 再起動時のクロバー。ログアウト/ログインで復活するか、"
echo "    別の再起動手順が必要。その旨を伝えてください。"
echo "  ・1 で残量が出ない → スクリプト側(python3/認証)の問題。出力を共有してください。"
echo "==================================================================="
