#!/usr/bin/env bash
# Provision a fresh Ubuntu/Debian host for the trading bot.
#
#   curl -fsSL <raw-url>/deploy/setup.sh | bash     # or clone first and run it
#
# Idempotent: safe to re-run. Verifies each step rather than assuming, because a
# half-provisioned host that looks fine is worse than one that failed loudly.
set -euo pipefail

BOT_DIR="${BOT_DIR:-/opt/trading-bot}"
REPO="${REPO:-https://github.com/Dima252/Trading-Bot.git}"
PY="${BOT_DIR}/venv/bin/python"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '    \033[33m!!\033[0m  %s\n' "$*"; }
die()  { printf '    \033[31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

say "System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git tzdata
ok "$(python3 --version)"

say "Repository at ${BOT_DIR}"
if [ -d "${BOT_DIR}/.git" ]; then
  git -C "${BOT_DIR}" pull --ff-only && ok "updated"
else
  sudo mkdir -p "$(dirname "${BOT_DIR}")"
  sudo chown "$(id -u):$(id -g)" "$(dirname "${BOT_DIR}")"
  git clone --quiet "${REPO}" "${BOT_DIR}" && ok "cloned"
fi
cd "${BOT_DIR}"
mkdir -p logs out data

say "Virtualenv"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -e ".[dev,config]" alpaca-py requests
ok "dependencies installed"

say "Test suite"
# The suite needs no network and no credentials. If it fails here, stop --
# every number this system produces depends on it.
"${PY}" -m pytest -q || die "tests failed; not proceeding"

say "Credentials"
if [ ! -f .env ]; then
  cat > .env <<'ENVEOF'
# Alpaca PAPER credentials -- generate at alpaca.markets with the dashboard
# switched to Paper Trading. Paper and live have SEPARATE key pairs, and paper
# keys cannot authenticate against the live endpoint, which is a useful safety
# property: a bug that tried to trade for real would simply fail.
APCA_API_KEY_ID=
APCA_API_SECRET_KEY=

# Strongly advised. Without a heartbeat, a job that never runs raises no alarm
# at all -- dead code cannot report that it is dead.
# TRADING_BOT_WEBHOOK=https://hooks.slack.com/services/...
# TRADING_BOT_HEARTBEAT=https://hc-ping.com/your-uuid
ENVEOF
  chmod 600 .env
  warn "created .env -- fill in your keys, then re-run this script"
  exit 0
fi
chmod 600 .env
grep -q 'APCA_API_KEY_ID=.\+' .env || die ".env has no APCA_API_KEY_ID"
ok ".env present and readable only by you"

say "Broker connectivity"
"${PY}" - <<'PYEOF' || die "could not reach Alpaca"
from trading_bot.cli import load_env
load_env()
from trading_bot.broker.alpaca import AlpacaBroker
a = AlpacaBroker(paper=True).account()
print(f"    connected: equity ${a.equity:,.2f}, cash ${a.cash:,.2f}")
PYEOF
ok "paper account reachable"

say "Market data"
# Live only needs ~500 sessions of history: MIN_HISTORY is 220 bars and the
# evening scan looks back 500 days. The 33 years in the research cache exists
# for backtesting and is not worth transferring -- the default fetch is plenty.
BARS=$("${PY}" -c "from trading_bot.data.cache import BarCache; print(BarCache('data/bars.db').stats()['bars'])" 2>/dev/null || echo 0)
if [ "${BARS}" -lt 100000 ]; then
  warn "cache is thin (${BARS} bars) -- fetching, this takes ~10 minutes"
  "${PY}" -m trading_bot fetch
else
  ok "${BARS} bars cached"
fi

say "Earnings calendar"
# The only rule that CLOSES a position rather than declining to open one. With
# no calendar it cannot fire, and the design reads as though the risk is
# covered. Refreshed weekly by cron; this is the first fill.
DATES=$("${PY}" -c "import json,os; print(len(json.load(open('config/earnings.json'))) if os.path.exists('config/earnings.json') else 0)" 2>/dev/null || echo 0)
if [ "${DATES}" -lt 50 ]; then
  warn "calendar has ${DATES} dates -- fetching"
  "${PY}" -m trading_bot earnings || warn "earnings fetch failed; the event rule will not fire until it succeeds"
else
  ok "${DATES} earnings dates"
fi

say "Alerting"
"${PY}" -m trading_bot status | sed -n '/alerting:/,/^$/p' || true

say "Schedule"
if crontab -l 2>/dev/null | grep -q trading_bot; then
  ok "crontab already installed"
else
  warn "not installed. Review then apply:"
  echo "      sed 's#/opt/trading-bot#${BOT_DIR}#' deploy/crontab.template | crontab -"
fi

say "Done"
cat <<EOF
    Dashboard:  ${PY} -m trading_bot serve --token <pick-one>
    From your laptop:  ssh -L 8080:localhost:8080 $(whoami)@\$(hostname -I | awk '{print \$1}')

    Start with TWO WEEKS of dry runs before dropping the flag:
      ${PY} -m trading_bot evening --dry-run

    The stopping rule is in PLAN.md section 5d. Read it before going live.
EOF
