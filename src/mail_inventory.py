#!/usr/bin/env python3
"""
Inventario de un buzón: qué carpetas hay y cuánto pesa cada una.

Objetivo: dimensionar la acumulación ANTES de diseñar reglas de organización.
No modifica nada (todo en readonly).

Uso:
    python3 mail_inventory.py                      # carpetas de todas las cuentas
    python3 mail_inventory.py --account personal
    python3 mail_inventory.py --senders 2000       # + top remitentes de INBOX
    python3 mail_inventory.py --year 2024          # + recuento por remitente de un año
"""

from __future__ import annotations

import argparse
import collections
import email
import email.utils
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mail_scan as ms  # reutiliza conexión, config y decodificación


def list_folders(ctx) -> list[tuple[str, str]]:
    """Devuelve [(nombre, atributos)]."""
    typ, data = ctx.list()
    out = []
    if typ != "OK":
        return out
    for line in data or []:
        if not line:
            continue
        s = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
        m = re.match(r'\((?P<attr>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)$', s)
        if not m:
            continue
        name = m.group("name")
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        out.append((name, m.group("attr")))
    return out


def folder_counts(ctx, folders) -> dict:
    res = {}
    for name, attr in folders:
        try:
            typ, data = ctx.status(f'"{name}"', "(MESSAGES UNSEEN)")
            if typ != "OK" or not data:
                continue
            s = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
            m_total = re.search(r"MESSAGES (\d+)", s)
            m_unseen = re.search(r"UNSEEN (\d+)", s)
            res[name] = {
                "messages": int(m_total.group(1)) if m_total else None,
                "unseen": int(m_unseen.group(1)) if m_unseen else None,
                "special": "\\Noselect" in attr,
            }
        except Exception as e:
            res[name] = {"error": f"{type(e).__name__}: {e}"}
    return res


def sender_stats(ctx, folder: str, limit: int, since: str | None = None) -> dict:
    """Top remitentes y top dominios de una carpeta (sin descargar cuerpos)."""
    typ, _ = ctx.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        return {"error": f"no se pudo abrir {folder}"}
    criteria = f'(SINCE "{since}")' if since else "ALL"
    typ, data = ctx.uid("SEARCH", None, criteria)
    if typ != "OK":
        return {"error": f"SEARCH falló en {folder}"}
    uids = (data[0] or b"").split()
    total = len(uids)
    sample = uids[-limit:]
    senders = collections.Counter()
    domains = collections.Counter()
    years = collections.Counter()
    B = 500
    for i in range(0, len(sample), B):
        chunk = b",".join(sample[i:i + B])
        typ, fetched = ctx.uid("FETCH", chunk.decode(),
                               "(BODY.PEEK[HEADER.FIELDS (FROM DATE)])")
        if typ != "OK":
            continue
        for it in fetched:
            if not isinstance(it, tuple) or len(it) < 2:
                continue
            try:
                msg = email.message_from_bytes(it[1])
            except Exception:
                continue
            _, addr = ms.parse_addr(msg.get("From", ""))
            name = ms.decode_header_value((msg.get("From", "") or "").split("<")[0].strip()) or addr
            if addr:
                senders[f"{name[:40]} <{addr}>"] += 1
                domains[addr.split("@")[-1]] += 1
            try:
                dt = email.utils.parsedate_to_datetime(msg.get("Date", ""))
                if dt:
                    years[dt.year] += 1
            except Exception:
                pass
    return {
        "folder": folder,
        "total_messages": total,
        "sampled": len(sample),
        "top_senders": senders.most_common(25),
        "top_domains": domains.most_common(20),
        "by_year": sorted(years.items()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Inventario IMAP de carpetas y remitentes.")
    ap.add_argument("--account", default=None)
    ap.add_argument("--senders", type=int, default=0,
                    help="analiza el top de remitentes sobre los últimos N mensajes")
    ap.add_argument("--folder", default="INBOX")
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    accounts = ms.load_config()
    if args.account:
        needle = args.account.lower()
        accounts = [a for a in accounts
                    if needle in (a.get("id", "").lower(), a.get("email", "").lower())]
        if not accounts:
            raise SystemExit(f"Ninguna cuenta coincide con '{args.account}'.")

    out = {"accounts": []}
    for acc in accounts:
        entry = {"id": acc.get("id"), "email": acc.get("email")}
        try:
            ctx = ms.connect(acc)
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            out["accounts"].append(entry)
            print(f"!! {entry['email']}: {entry['error']}", file=sys.stderr)
            continue
        try:
            folders = list_folders(ctx)
            entry["folders"] = folder_counts(ctx, folders)
            if args.senders:
                since = f"01-Jan-{args.year}" if args.year else None
                entry["senders"] = sender_stats(ctx, args.folder, args.senders, since)
        finally:
            try:
                ctx.logout()
            except Exception:
                pass
        out["accounts"].append(entry)

    if args.json:
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    for a in out["accounts"]:
        print(f"\n{'='*70}\n{a['email']}  (id={a['id']})")
        if a.get("error"):
            print("  ERROR:", a["error"]); continue
        print(f"{'-'*70}\n  CARPETAS")
        items = [(n, v) for n, v in (a.get("folders") or {}).items()]
        items.sort(key=lambda kv: -(kv[1].get("messages") or 0))
        for name, v in items:
            if v.get("error"):
                print(f"    {name:44s} error: {v['error'][:50]}")
            elif v.get("special"):
                print(f"    {name:44s} (no seleccionable)")
            else:
                print(f"    {name:44s} {v['messages']:>8} msgs  ({v['unseen']} sin leer)")
        s = a.get("senders")
        if s and not s.get("error"):
            print(f"\n  REMITENTES (de {s['total_messages']} en {s['folder']}, "
                  f"muestra {s['sampled']})")
            for who, n in s["top_senders"]:
                print(f"    {n:>6}  {who}")
            print("\n  DOMINIOS")
            for d, n in s["top_domains"]:
                print(f"    {n:>6}  {d}")
            print("\n  POR AÑO")
            for y, n in s["by_year"]:
                print(f"    {y}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())