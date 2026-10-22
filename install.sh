#!/usr/bin/env bash
#
# Instalador de mail-monitor para una instancia de Hermes Agent (u otra máquina
# con Python 3.10+). Deja los scripts, la configuración de ejemplo y las
# variables de entorno listos para usar.
#
# Uso:
#   ./install.sh                     # instalación interactiva
#   ./install.sh --prefix ~/.hermes  # fija $HERMES_HOME
#   ./install.sh --dry-run           # muestra lo que haría, sin tocar nada
#   ./install.sh --with-cron         # además, deja los cron de ejemplo listos
#
# No instala dependencias: el proyecto usa solo la biblioteca estándar de Python.
# Nunca escribe credenciales. Solo crea la estructura y copia plantillas.

set -euo pipefail

# ---------------------------------------------------------------------------
# Presentación
# ---------------------------------------------------------------------------
say()  { printf '\033[1;36m%s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Valores por defecto
# ---------------------------------------------------------------------------
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DRY_RUN=0
WITH_CRON=0
INSTALL_DIR=""
PYTHON_BIN=""

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

# ---------------------------------------------------------------------------
# Argumentos
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)     HERMES_HOME="$2"; shift 2 ;;
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    --python)     PYTHON_BIN="$2"; shift 2 ;;
    --with-cron)  WITH_CRON=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    usage ;;
    *) die "Opción desconocida: $1 (usa --help)" ;;
  esac
done

HERMES_HOME="${HERMES_HOME/#\~/$HOME}"
INSTALL_DIR="${INSTALL_DIR:-$HERMES_HOME/scripts}"

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '    [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# ---------------------------------------------------------------------------
# 1. Comprobar Python
# ---------------------------------------------------------------------------
say "1/6  Comprobando Python"
if [[ -z "$PYTHON_BIN" ]]; then
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then PYTHON_BIN="$cand"; break; fi
  done
fi
[[ -n "$PYTHON_BIN" ]] || die "No se encontró Python 3. Instálalo y vuelve a intentarlo."

PYVER="$("$PYTHON_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
"$PYTHON_BIN" -c 'import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)' \
  || die "Se necesita Python 3.10 o superior (encontrado $PYVER)."
"$PYTHON_BIN" -c 'import imaplib, ssl, email, json' \
  || die "A tu Python le faltan módulos de la biblioteca estándar (imaplib/ssl/email)."
ok "Python $PYVER en $(command -v "$PYTHON_BIN")"

# ---------------------------------------------------------------------------
# 2. Comprobar el árbol de origen
# ---------------------------------------------------------------------------
say "2/6  Comprobando el proyecto de origen"
for f in mail_scan.py mail_inventory.py mail_organize.py mail_purge.py ms_oauth.py; do
  [[ -f "$SRC_DIR/src/$f" ]] || die "Falta src/$f. ¿Estás ejecutando el instalador desde la raíz del repo?"
done
ok "Los 5 scripts están presentes en src/"

# ---------------------------------------------------------------------------
# 3. Crear la estructura bajo $HERMES_HOME
# ---------------------------------------------------------------------------
say "3/6  Creando la estructura en $HERMES_HOME"
run mkdir -p "$HERMES_HOME/secrets" "$HERMES_HOME/rules" "$HERMES_HOME/state" "$INSTALL_DIR"
run chmod 700 "$HERMES_HOME/secrets"
ok "$HERMES_HOME/{secrets,rules,state} listos (secrets en 700)"

# ---------------------------------------------------------------------------
# 4. Copiar scripts
# ---------------------------------------------------------------------------
say "4/6  Copiando scripts a $INSTALL_DIR"
for f in mail_scan.py mail_inventory.py mail_organize.py mail_purge.py ms_oauth.py; do
  run cp "$SRC_DIR/src/$f" "$INSTALL_DIR/$f"
  run chmod 644 "$INSTALL_DIR/$f"
done
run cp "$SRC_DIR/src/mail_cycle.sh" "$INSTALL_DIR/mail_cycle.sh"
run chmod 755 "$INSTALL_DIR/mail_cycle.sh"
ok "Scripts copiados (mail_cycle.sh ejecutable)"

# ---------------------------------------------------------------------------
# 5. Configuración de ejemplo (nunca sobreescribe lo que ya exista)
# ---------------------------------------------------------------------------
say "5/6  Preparando configuración de ejemplo"
copy_example() {
  local src="$1" dst="$2"
  if [[ -f "$dst" ]]; then
    warn "Ya existe $(basename "$dst") — no se toca"
  else
    run cp "$src" "$dst"
    ok "$(basename "$dst") creado (edítalo con tus datos)"
  fi
}
copy_example "$SRC_DIR/config/mail_accounts.example.json" "$HERMES_HOME/secrets/mail_accounts.json"
copy_example "$SRC_DIR/config/mail_rules.example.json"    "$HERMES_HOME/rules/mail_rules.json"
copy_example "$SRC_DIR/config/mail_purge.example.json"    "$HERMES_HOME/rules/mail_purge.json"
run chmod 600 "$HERMES_HOME/secrets/mail_accounts.json"

# ---------------------------------------------------------------------------
# 6. Variables de entorno y cron opcional
# ---------------------------------------------------------------------------
say "6/6  Terminando"
ENV_HINT="export HERMES_HOME=\"$HERMES_HOME\""
if [[ "$WITH_CRON" -eq 1 ]]; then
  cat <<CRON
  Añade esto a tu crontab (crontab -e):

  # Vigilante: escanea y avisa (ejemplo 8:00, 13:00 y 19:00)
  0 8,13,19 * * * HERMES_HOME=$HERMES_HOME $PYTHON_BIN $INSTALL_DIR/mail_scan.py >/dev/null

  # Organizador: mueve a carpetas lo que ya no es reciente
  0 9,14,20 * * * HERMES_HOME=$HERMES_HOME MIN_AGE_HOURS=12 $INSTALL_DIR/mail_cycle.sh >/dev/null
CRON
else
  echo "  Para programarlo, vuelve a ejecutar con --with-cron, o mira el README."
fi

cat <<EOF

$(say "Listo. Siguientes pasos:")

  1. $ENV_HINT
  2. Edita $HERMES_HOME/secrets/mail_accounts.json con tus cuentas reales.
  3. Guarda la contraseña de aplicación (Gmail) en un archivo 600:
       read -rsp 'App password: ' P && printf '%s' "\$P" > $HERMES_HOME/secrets/apppass_gmail_personal \\
         && chmod 600 $HERMES_HOME/secrets/apppass_gmail_personal && unset P
  4. Para Outlook/Microsoft personal, usa OAuth2 (no hay contraseña):
       $PYTHON_BIN $INSTALL_DIR/ms_oauth.py start
       $PYTHON_BIN $INSTALL_DIR/ms_oauth.py finish
  5. Comprueba las conexiones (no modifica nada):
       $PYTHON_BIN $INSTALL_DIR/mail_scan.py --check
  6. Dimensiona el buzón antes de diseñar reglas:
       $PYTHON_BIN $INSTALL_DIR/mail_inventory.py

  Recuerda: mail_organize.py y mail_purge.py SIMULAN por defecto.
  Solo actúan con --apply. La purga mueve a la papelera, nunca borra de verdad.
EOF
