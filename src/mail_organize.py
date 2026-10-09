#!/usr/bin/env python3
"""
Aplica las reglas de agrupación de ~/.hermes/rules/mail_rules.json.

SEGURO POR DEFECTO: sin --apply solo simula y reporta. No crea carpetas,
no mueve y no borra nada.

Las reglas son POR CUENTA (bloques "profiles"): las de Gmail no se aplican
a Outlook. Un perfil declara qué cuentas cubre y de qué carpeta lee.

Los nombres de carpeta se escriben LEGIBLES en el JSON (con acentos) y el
script los traduce a IMAP modified UTF-7 al hablar con el servidor, que es
como los devuelve el LIST (Créditos -> Cr&AOk-ditos).

Uso:
    python3 mail_organize.py                          # simulacro (todo)
    python3 mail_organize.py --account outlook       # una cuenta
    python3 mail_organize.py --rule promo-buscalibre  # una sola regla
    python3 mail_organize.py --apply                  # ejecuta de verdad
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mail_scan as ms

HERMES_HOME = Path(__import__("os").environ.get("HERMES_HOME", Path.home() / ".hermes"))
RULES_PATH = HERMES_HOME / "rules" / "mail_rules.json"


# ---------------------------------------------------------------- IMAP UTF-7
def utf7_decode(s: str) -> str:
    """Decodifica IMAP modified UTF-7 (RFC 3501): 'Cr&AOk-ditos' -> 'Créditos'.

    El servidor siempre habla en esta forma, así que se decodifica al leer
    y se codifica al escribir, y las reglas se mantienen legibles.
    """
    if not s or "&" not in s:
        return s
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch == "&":
            j = s.find("-", i)
            if j == -1:                      # '&' suelto al final
                out.append(s[i:])
                break
            chunk = s[i + 1:j]
            if chunk == "":
                out.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * ((4 - len(b64) % 4) % 4)
                try:
                    out.append(base64.b64decode(b64).decode("utf-16-be"))
                except Exception:
                    out.append(chunk)
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def utf7_encode(s: str) -> str:
    """Codifica a IMAP modified UTF-7 solo lo que no sea ASCII imprimible."""
    out, buf = [], []

    def flush():
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            b64 = base64.b64encode(raw).decode("ascii").rstrip("=")
            out.append("&" + b64.replace("/", ",") + "-")
            buf.clear()

    for ch in s:
        if ch == "&":
            flush()
            out.append("&-")
        elif 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append(ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def load_rules() -> dict:
    if not RULES_PATH.exists():
        raise SystemExit(f"No existe {RULES_PATH}")
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def profile_for(data: dict, acc: dict) -> dict | None:
    """Devuelve el perfil que cubre esta cuenta, o None si ninguno lo hace."""
    for prof in data.get("profiles", []):
        ids = [str(x).lower() for x in prof.get("accounts", [])]
        if acc.get("id", "").lower() in ids or acc.get("email", "").lower() in ids:
            return prof
    return None


def list_folders(ctx) -> set[str]:
    """Nombres de carpeta DECODIFICADOS, para comparar con las reglas."""
    typ, data = ctx.list()
    out = set()
    if typ != "OK":
        return out
    for line in data or []:
        s = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
        m = re.match(r'\([^)]*\)\s+"[^"]*"\s+(?P<name>.+)$', s)
        if not m:
            continue
        name = m.group("name")
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        out.add(utf7_decode(name))
    return out


def uids_for_rule(ctx, rule: dict, sources: list[str], claimed: set[int]) -> list[int]:
    """UIDs que matchean la regla y aún no han sido reclamados por otra.

    'claimed' es seguro aquí porque todas las reglas de un perfil leen de las
    MISMAS carpetas fuente: los UIDs son únicos dentro de cada carpeta.
    """
    pats = list(rule.get("match", {}).get("domain", []))
    pats += list(rule.get("match", {}).get("from", []))
    got: list[int] = []
    for folder in sources:
        typ, _ = ctx.select(f'"{utf7_encode(folder)}"', readonly=True)
        if typ != "OK":
            continue
        for pat in pats:
            try:
                typ, d = ctx.uid("SEARCH", None, f'(FROM "{pat}")')
            except Exception:
                continue
            if typ != "OK":
                continue
            for u in (d[0] or b"").split():
                ui = int(u)
                if ui not in claimed:
                    claimed.add(ui)
                    got.append(ui)
    return got


def sample_headers(ctx, folder: str, uids: list[int], n: int = 3) -> list[str]:
    if not uids:
        return []
    ctx.select(f'"{utf7_encode(folder)}"', readonly=True)
    typ, fetched = ctx.uid("FETCH", ",".join(str(u) for u in uids[:n]),
                           "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
    out = []
    if typ != "OK":
        return out
    for it in fetched:
        if not isinstance(it, tuple) or len(it) < 2:
            continue
        try:
            import email as _email
            msg = _email.message_from_bytes(it[1])
        except Exception:
            continue
        frm = ms.decode_header_value(msg.get("From", ""))[:44]
        sub = ms.decode_header_value(msg.get("Subject", ""))[:52]
        dt = (msg.get("Date") or "")[:22]
        out.append(f"{dt} | {frm} | {sub}")
    return out


def move_uids(ctx, uids: list[int], dest: str, batch: int) -> tuple[int, str | None]:
    """Mueve por MOVE (RFC 6851) si el servidor lo soporta; si no, COPY+Delete+EXPUNGE."""
    caps = getattr(ctx, "capabilities", ())
    has_move = "MOVE" in caps
    has_uidplus = "UIDPLUS" in caps
    quoted = f'"{utf7_encode(dest)}"'
    moved = 0
    for i in range(0, len(uids), batch):
        chunk = uids[i:i + batch]
        uid_set = ",".join(str(u) for u in chunk)
        try:
            if has_move:
                typ, _ = ctx.uid("MOVE", uid_set, quoted)
                if typ != "OK":
                    return moved, "MOVE devolvió distinto de OK"
                moved += len(chunk)
            else:
                typ, _ = ctx.uid("COPY", uid_set, quoted)
                if typ != "OK":
                    return moved, "COPY devolvió distinto de OK"
                ctx.uid("STORE", uid_set, "+FLAGS.SILENT", r"(\Deleted)")
                if has_uidplus:
                    ctx.uid("EXPUNGE", uid_set)
                moved += len(chunk)
        except Exception as e:
            return moved, f"{type(e).__name__}: {e}"
    return moved, None


def main() -> int:
    ap = argparse.ArgumentParser(description="Agrupa correo en carpetas según reglas.")
    ap.add_argument("--account", default=None)
    ap.add_argument("--rule", default=None, help="aplica solo esta regla")
    ap.add_argument("--profile", default=None, help="aplica solo este perfil")
    ap.add_argument("--apply", action="store_true",
                    help="EJECUTA los cambios (por defecto solo simula)")
    ap.add_argument("--batch", type=int, default=300)
    args = ap.parse_args()

    data = load_rules()
    if not args.apply:
        print("*** SIMULACRO — no se modifica nada. Añade --apply para ejecutar. ***\n")

    accounts = ms.load_config()
    if args.account:
        needle = args.account.lower()
        accounts = [a for a in accounts
                    if needle in (a.get("id", "").lower(), a.get("email", "").lower())]
        if not accounts:
            raise SystemExit(f"Ninguna cuenta coincide con '{args.account}'.")

    grand_total = 0
    for acc in accounts:
        print(f"\n{'='*74}\n{acc.get('email')}")
        prof = profile_for(data, acc)
        if not prof:
            print("  (sin perfil de reglas para esta cuenta; se omite)")
            continue
        if args.profile and prof.get("id") != args.profile:
            continue
        sources = prof.get("source_folders", ["INBOX"])
        rules = prof.get("rules", [])
        if args.rule:
            rules = [r for r in rules if r["id"] == args.rule]
            if not rules:
                print(f"  (la regla '{args.rule}' no existe en el perfil "
                      f"'{prof.get('id')}')")
                continue
        print(f"  perfil: {prof.get('id')} | origen: {', '.join(sources)} | "
              f"{len(rules)} reglas")
        try:
            ctx = ms.connect(acc)
        except Exception as e:
            print(f"  ERROR de conexión: {type(e).__name__}: {e}")
            continue
        try:
            existing = list_folders(ctx)
            claimed: set[int] = set()
            for rule in rules:
                dest = rule["to"]
                uids = uids_for_rule(ctx, rule, sources, claimed)
                if not uids:
                    continue
                exists = dest in existing
                flag = "" if exists else (
                    "  [crear]" if rule.get("create") else "  [FALTA y no crea]")
                status = ""
                if args.apply:
                    if not exists:
                        if rule.get("create"):
                            try:
                                ctx.create(f'"{utf7_encode(dest)}"')
                                existing.add(dest)
                                status = "carpeta creada; "
                            except Exception as e:
                                print(f"  !! {rule['id']}: no pude crear {dest}: {e}")
                                continue
                        else:
                            print(f"  !! {rule['id']}: destino inexistente {dest}; omitido")
                            continue
                    moved, err = move_uids(ctx, uids, dest, args.batch)
                    status += f"movidos {moved}" + (f" — ERROR: {err}" if err else "")
                    grand_total += moved
                print(f"\n  [{rule['id']}]{flag}")
                print(f"    {len(uids):>5} correos -> {dest}")
                if status:
                    print(f"    {status}")
                for line in sample_headers(ctx, sources[0], uids):
                    print(f"      · {line}")
            typ, _ = ctx.select(f'"{utf7_encode(sources[0])}"', readonly=True)
            typ, d = ctx.uid("SEARCH", None, "ALL")
            total_src = len((d[0] or b"").split())
            nombre = sources[0]
            if args.apply:
                print(f"\n  --- {nombre} restante tras mover: {total_src} mensajes")
            else:
                print(f"\n  --- {nombre}: {total_src} mensajes | capturados por reglas: "
                      f"{len(claimed)} | quedarían sin tocar: {total_src - len(claimed)}")
        finally:
            try:
                ctx.logout()
            except Exception:
                pass

    if args.apply:
        print(f"\nTOTAL movidos: {grand_total}")
    else:
        print("\nFIN DEL SIMULACRO. Nada se ha modificado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())