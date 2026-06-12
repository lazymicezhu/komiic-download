#!/bin/zsh
set -u

PROJECT_DIR="/Users/lazymice/Documents/kommic漫画下载器"
URL="http://127.0.0.1:8787"
LOG_FILE="$PROJECT_DIR/gui_server.log"
PYTHON="$PROJECT_DIR/.venv/bin/python"
LABEL="komiic-gui"

cd "$PROJECT_DIR" || exit 1

if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

if [[ -z "$PYTHON" ]]; then
  echo "找不到 Python，请先安装 Python 3 或创建项目虚拟环境。"
  if [[ -t 0 ]]; then
    read -k 1 "reply?按任意键关闭..."
  fi
  exit 1
fi

is_running() {
  curl --max-time 2 -fsS "$URL/api/state" >/dev/null 2>&1
}

if ! is_running; then
  launchctl remove "$LABEL" >/dev/null 2>&1 || true
  : >"$LOG_FILE"
  launchctl submit -l "$LABEL" -- /bin/sh -c "cd '$PROJECT_DIR' && exec '$PYTHON' '$PROJECT_DIR/gui_server.py' >>'$LOG_FILE' 2>&1" >>"$LOG_FILE" 2>&1 &!
  for _ in {1..30}; do
    if is_running; then
      break
    fi
    sleep 0.3
  done
  if ! is_running && [[ -z "${CODEX_CI:-}" ]]; then
    nohup "$PYTHON" "$PROJECT_DIR/gui_server.py" >>"$LOG_FILE" 2>&1 &!
    for _ in {1..30}; do
      if is_running; then
        break
      fi
      sleep 0.3
    done
  fi
fi

if ! is_running; then
  echo "GUI 启动失败，最近日志："
  echo "可能原因：8787 端口被占用、Python/依赖不可用，或 launchctl 启动失败。"
  echo "----------------------------------------"
  tail -n 80 "$LOG_FILE" 2>/dev/null || true
  echo "----------------------------------------"
  if [[ -t 0 ]]; then
    read -k 1 "reply?按任意键关闭..."
  fi
  exit 1
fi

if [[ -n "${CODEX_CI:-}" ]]; then
  echo "$URL"
elif [[ -t 0 ]]; then
  open "$URL" >/dev/null 2>&1 &!
else
  echo "$URL"
fi
