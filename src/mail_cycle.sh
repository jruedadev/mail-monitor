#!/usr/bin/env bash
# Organiza el correo de TODAS las cuentas con perfil de reglas, dejando en el
# INBOX lo que aún es demasiado reciente para haber sido avisado.
#
# Pensado para ejecutarse desatendido desde un planificador. En modo --quiet
# no imprime nada salvo que haya movido algo o haya habido un error, así que
# un stdout vacío significa "todo igual" y el planificador no envía nada.
#
# MIN_AGE_HOURS: antigüedad mínima para mover un correo (por defecto 12 h).
# El vigilante revisa a las 8:00, 13:00 y 19:00; con 12 h de margen, todo
# correo nuevo tiene al menos una revisión por delante antes de ser movido.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$DIR/mail_organize.py" \
  --apply \
  --min-age-hours "${MIN_AGE_HOURS:-12}" \
  --quiet
