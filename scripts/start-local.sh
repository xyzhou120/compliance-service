#!/usr/bin/env bash
# 一键启动本地合规检查服务（macOS / Linux）。
#   用法: scripts/start-local.sh [start|stop|status]
# 开关配置写在 config/pipeline.local.toml，不需要环境变量。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PORT="${PORT:-8080}"
HOST="${HOST:-127.0.0.1}"
DB="${DB:-.local/service.sqlite3}"
PIDS=".local/start-local.pids"
PY="$ROOT/.venv/bin/python"

say() { printf '%s\n' "$*"; }
fail() { printf '错误: %s\n' "$*" >&2; exit 1; }

ensure_python() {
  if [ ! -x "$PY" ]; then
    say "==> 创建虚拟环境 .venv"
    python3 -m venv .venv
    "$ROOT/.venv/bin/python" -m pip install -q --upgrade pip
  fi
  if ! "$PY" -c 'import fastapi' >/dev/null 2>&1; then
    say "==> 安装依赖（首次较慢）"
    "$PY" -m pip install -q -e ".[test]"
  fi
}

ensure_config() {
  mkdir -p config .local
  if [ ! -f config/pipeline.local.toml ]; then
    cp config/pipeline.example.toml config/pipeline.local.toml
    say "==> 已生成 config/pipeline.local.toml（page_receipts / canonical_facts 已开启）"
  fi
  if [ ! -f config/models.local.toml ]; then
    say ""
    say "缺少 config/models.local.toml —— 请先复制模板并填写内网地址："
    say "  cp config/models.example.toml config/models.local.toml"
    say "  需要填: [ocr] base_url 指向 OCR 服务, [vision] base_url/model/api_key"
    say ""
    exit 1
  fi
}

case "${1:-start}" in
  stop)
    if [ -f "$PIDS" ]; then
      while read -r pid; do kill "$pid" 2>/dev/null || true; done < "$PIDS"
      rm -f "$PIDS"
      say "==> 已停止本脚本启动的进程"
    else
      say "没有记录到本脚本启动的进程"
    fi
    exit 0
    ;;
  status)
    for name in api worker; do
      pid=$(grep -m1 "^$name " "$PIDS" 2>/dev/null | awk '{print $2}' || true)
      if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
        say "$name 运行中 (pid $pid)"
      else
        say "$name 未运行"
      fi
    done
    exit 0
    ;;
  start) ;;
  *) fail "未知命令: ${1}" ;;
esac

ensure_python
ensure_config

CFG=()
if [ -f config/local-debug.toml ]; then
  CFG=(--production-config config/local-debug.toml)
  say "==> 使用 config/local-debug.toml（OceanBase/附件适配器将启用）"
else
  say "==> 未发现 config/local-debug.toml：本地模式（触发页 OceanBase 选项不可选）"
fi

say "==> 初始化账本 $DB"
"$PY" -m compliance_service "${CFG[@]}" --db "$DB" init >/dev/null

: > "$PIDS"
say "==> 启动 worker"
nohup "$PY" -m compliance_service "${CFG[@]}" --db "$DB" worker > .local/worker.log 2>&1 &
echo "worker $!" >> "$PIDS"

say "==> 启动 API http://$HOST:$PORT"
nohup "$PY" -m compliance_service "${CFG[@]}" --db "$DB" serve --host "$HOST" --port "$PORT" > .local/api.log 2>&1 &
echo "api $!" >> "$PIDS"

PORT_BUSY=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)
if [ -n "${PORT_BUSY:-}" ]; then
  say "端口 $PORT 已被进程 $PORT_BUSY 占用。"
  say "  若是旧实例：kill $PORT_BUSY 后重试"
  say "  或换端口：PORT=8081 scripts/start-local.sh"
  say "注意：旧实例通常是不带 --production-config 启动的，OceanBase 选项会一直不可选。"
  exit 1
fi

say "==> 等待健康检查"
for _ in $(seq 1 40); do
  if curl -fsS "http://$HOST:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 0.5
done
if ! curl -fsS "http://$HOST:$PORT/health" >/dev/null 2>&1; then
  say "API 未就绪，查看 .local/api.log"
  tail -20 .local/api.log || true
  exit 1
fi

say ""
say "服务已就绪："
say "  调试页面  http://$HOST:$PORT/debug"
say "  任务接口  POST http://$HOST:$PORT/v1/manual/runs"
say "  日志      .local/api.log / .local/worker.log"
say "  停止      scripts/start-local.sh stop"
say ""
say "页面里粘贴这段即可触发一次合成检查："
cat <<'JSON'
{"sourceSystem":"demo","mineCode":"synthetic-mine","checkCycle":"2026-10",
 "scope":{"fixture":"clean"},"callbackMode":"mock",
 "items":[{"itemCode":"stop_work_unload"},{"itemCode":"relief_effects"}]}
JSON
say ""
say "跑真实文件：把 scope 换成 manifest 绝对路径（格式见 docs/local-task-debug.md）"
