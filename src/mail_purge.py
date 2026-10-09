#!/usr/bin/env python3
"""
Purga: mueve correo a la Papelera, según ~/.hermes/rules/mail_purge.json.

NUNCA borra de forma definitiva: el destino es la Papelera del proveedor
(en Gmail se vacía sola a los 30 días, así que durante ese mes todo se
recupera). El script no hace EXPUNGE ni borra carpetas.

SEGURO POR DEFECTO: sin --apply solo simula y reporta.

Uso:
    python3 mail_purge.py --plan grupo-a                       # simulacro
    python3 mail_purge.py --plan grupo-a --account personal
    python3 mail_purge.py --plan grupo-a --apply               # ejecuta
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mail_scan as ms
import mail_organize as mo

HERMES_HOME = Path(__import__("os").environ.get("HERMES_HOME", Path.home() / ".hermes"))
PURGE_PATH = HERMES_HOME / "rules" / "mail_purge.json"


def load_plan() -> dict:
    if not PURGE_PATH.exists():
        raise SystemExit(f"No existe {PURGE_PATH}")
    return json.loads(PURGE_PATH.read_text(encoding="utf-8"))


def count_uids(ctx, folder: str, before: int | None) -> list[int]:
    typ, _ = ctx.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        raise RuntimeError(f"no se pudo abrir {folder}")
    criteria = f'(BEFORE "01-Jan-{before}")' if before else "ALL"
    typ, d = ctx.uid("SEARCH", None, criteria)
    if typ != "OK":
        raise RuntimeError(f"SEARCH falló en {folder}")
    return [int(u) for u in (d[0] or b"").split()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Mueve correo a la Papelera según plan.")
    ap.add_argument("--plan", required=True, help="id del plan en mail_purge.json")
    ap.add_argument("--account", default=None)
    ap.add_argument("--apply", action="store_true",
                    help="EJECUTA (por defecto solo simula)")
    ap.add_argument("--batch", type=int, default=300)
    ap.add_argument("--limit", type=int, default=0, help="tope por carpeta (0 = todo)")
    args = ap.parse_args()

    cfg = load_plan()
    trash = cfg["trash_folder"]
    if args.plan not in cfg.get("plans", {}):
        raise SystemExit(f"Plan desconocido: {args.plan}. "
                         f"Disponibles: {', '.join(cfg.get('plans', {}))}")
    plan = cfg["plans"][args.plan]

    print(f"Plan: {args.plan}")
    print(f"Motivo: {plan.get('why', '—')}")
    print(f"Destino: {trash}  (NO es borrado definitivo)")
    if not args.apply:
        print("\n*** SIMULACRO — no se mueve nada. Añade --apply para ejecutar. ***\n")

    accounts = ms.load_config()
    if args.account:
        needle = args.account.lower()
        accounts = [a for a in accounts
                    if needle in (a.get("id", "").lower(), a.get("email", "").lower())]
        if not accounts:
            raise SystemExit(f"Ninguna cuenta coincide con '{args.account}'.")

    grand = 0
    for acc in accounts:
        print(f"\n{'='*72}\n{acc.get('email')}")
        try:
            ctx = ms.connect(acc)
        except Exception as e:
            print(f"  ERROR de conexión: {type(e).__name__}: {e}")
            continue
        try:
            existing = mo.list_folders(ctx)
            if trash not in existing:
                print(f"  !! La Papelera '{trash}' no existe con ese nombre. Carpetas "
                      f"que contienen 'Papelera' o 'Trash': "
                      f"{[f for f in existing if 'papelera' in f.lower() or 'trash' in f.lower()]}")
                continue
            for item in plan["folders"]:
                path = item["path"]
                before = item.get("before")
                if path not in existing:
                    print(f"\n  [{path}] — no existe, se omite")
                    continue
                try:
                    uids = count_uids(ctx, path, before)
                except Exception as e:
                    print(f"\n  [{path}] error al buscar: {e}")
                    continue
                # OJO: los UIDs son únicos POR CARPETA, no por buzón. Deduplicar
                # entre carpetas distintas es incorrecto (daba 22 en vez de 187,
                # porque los números de Linio se comían los de Falabella).
                if args.limit:
                    uids = uids[:args.limit]
                etiqueta = f"anterior a {before}" if before else "toda la carpeta"
                print(f"\n  [{path}]  ({etiqueta})")
                print(f"    {len(uids):>5} correos -> {trash}")
                if not uids:
                    continue
                for line in mo.sample_headers(ctx, path, uids, 3):
                    print(f"      · {line}")
                if args.apply:
                    moved, err = mo.move_uids(ctx, uids, trash, args.batch)
                    print(f"    movidos {moved}" + (f" — ERROR: {err}" if err else ""))
                    grand += moved
        finally:
            try:
                ctx.logout()
            except Exception:
                pass

    if args.apply:
        print(f"\nTOTAL movidos a la Papelera: {grand}")
        print("Recuperables desde Gmail durante 30 días.")
    else:
        print("\nFIN DEL SIMULACRO. Nada se ha modificado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())