#!/usr/bin/env python3
"""
Escáner de correo para el asistente personal de Johan.  (v2, calibrada con datos reales)

Lee por IMAP las cuentas declaradas en ~/.hermes/secrets/mail_accounts.json,
detecta los mensajes NUEVOS desde la última ejecución y emite un JSON por
stdout con lo que merece atención.

Cambios de v2, tras el primer escaneo real:
  - Usa las etiquetas nativas de Gmail (CATEGORY_PROMOTIONS / _FORUMS) para
    detectar promociones, en vez de adivinar por el asunto.
  - Lista blanca financiera: Nequi / Bre-B / bancos son señal alta aunque el
    remitente sea un 'no-reply' (antes se perdían como ruido).
  - Las alertas de seguridad de Google que genera nuestro propio escáner se
    marcan con self_access_hint y se agrupan en vez de alertar una por una.
  - Se quita 'cuenta' del vocabulario de señal alta: daba falsos positivos
    ("cuenta regresiva", "cuenta atrás").
  - Modo --read para verificar un mensaje concreto antes de actuar.

Uso:
    python3 mail_scan.py                    # incremental (uso normal)
    python3 mail_scan.py --since-hours 48   # ventana amplia (primer escaneo)
    python3 mail_scan.py --reset            # olvida el estado y re-baselínea
    python3 mail_scan.py --account NAME     # limita a una cuenta (id o email)
    python3 mail_scan.py --max-per-folder 50
    python3 mail_scan.py --check            # solo valida credenciales
    python3 mail_scan.py --read 48082 --account personal [--folder INBOX]

Estado: ~/.hermes/state/mail_scan_state.json
Salida: JSON a stdout. Errores a stderr (nunca credenciales).
"""

from __future__ import annotations

import argparse
import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
CONFIG_PATH = HERMES_HOME / "secrets" / "mail_accounts.json"
STATE_PATH = HERMES_HOME / "state" / "mail_scan_state.json"

DEFAULT_FOLDERS = ["INBOX"]

# Remitentes/patrones que casi siempre son ruido. No se descartan: se marcan
# con probable_noise=True para que el agente los devalúe sin gastar contexto.
NOISE_SENDER_PATTERNS = [
    r"no[-_.]?reply@", r"noreply@", r"donotreply@", r"notification",
    r"newsletter", r"news@", r"marketing@", r"promo",
    r"mailer-daemon@", r"bounce", r"updates@", r"hello@",
]

# Promociones por asunto. Nota: X-GM-LABELS (CATEGORY_PROMOTIONS) vino vacío
# en las cuentas de Johan, así que no se puede confiar en la etiqueta nativa.
NOISE_SUBJECT_PROMO = [
    r"\d+\s*%\s*(off|dto|dscto|descuento)", r"cyberdays?", r"black friday",
    r"solo por hoy", r"no dejes pasar", r"[uú]ltimas horas", r"env[ií]o gratis",
    r"cup[oó]n", r"oferta", r"promoci[oó]n", r"rebajas", r"outlet",
    r"te quedan", r"expira hoy", r"hasta \d+\s*%", r"esta semana",
]

# Señal alta por asunto. Sin 'cuenta' ni 'alerta' sueltos: daban falsos
# positivos ("cuenta regresiva", "alerta de oferta").
HIGH_SIGNAL_SUBJECT = [
    r"factura", r"invoice", r"pago", r"payment", r"contrato", r"contract",
    r"urgente", r"urgent", r"vencim", r"deadline", r"seguridad", r"security",
    r"reuni[oó]n", r"entrevista", r"propuesta", r"cotiza", r"impuesto",
    r"dian", r"soporte", r"ticket", r"reclamo", r"p[oó]liza", r"renovaci[oó]n",
    r"desembolso", r"declaraci[oó]n", r"extracto",
]

# Dinero: manda sobre cualquier heurística de ruido. En el primer escaneo real
# "¡Recibiste plata por Bre-B!" de Nequi se perdió como ruido por venir de un
# 'notificaciones@'.
FINANCIAL_SENDERS = [
    r"nequi\.com", r"bancolombia", r"bancodebogota", r"davivienda", r"bbva",
    r"daviplata", r"bre-?b", r"banco", r"sura\.com", r"nu\.com", r"nubank",
    r"paypal", r"stripe", r"mercadopago", r"wompi", r"epayco", r"addi",
    r"colpatria", r"scotiabank", r"fiduciaria",
]
FINANCIAL_SUBJECTS = [
    r"recibiste", r"transferencia", r"abono", r"pago exitoso", r"cuota",
    r"desembolso", r"factura", r"retiro", r"consignaci", r"extracto",
]

# Avisos de acceso de Google: los genera nuestro propio escáner al conectar.
GOOGLE_SECURITY_SENDERS = [r"accounts\.google\.com"]
GOOGLE_SECURITY_SUBJECTS = [r"alerta de seguridad", r"security alert",
                            r"nuevo inicio de sesi", r"new sign-in"]

# Terceros que avisan de cambios de seguridad en cuentas del usuario.
SECURITY_THIRD_PARTY = [
    r"github\.com", r"gitlab\.com", r"microsoft\.com", r"apple\.com",
    r"aws\.amazon", r"amazon\.com", r"meta\.com", r"digitalocean",
    r"cloudflare", r"vercel", r"namecheap", r"godaddy",
]
SECURITY_THIRD_SUBJECTS = [
    r"oauth", r"password", r"contrase[nñ]a", r"inicio de sesi", r"sign-in",
    r"new login", r"2fa", r"two-factor", r"verificaci[oó]n",
    r"dispositivo desconocido", r"new device",
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def decode_header_value(raw) -> str:
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
    except Exception:
        return str(raw)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except Exception:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(str(chunk))
    return re.sub(r"\s+", " ", "".join(out)).strip()


def parse_addr(raw: str) -> tuple[str, str]:
    name, addr = email.utils.parseaddr(raw or "")
    return decode_header_value(name), (addr or "").lower()


def load_config() -> list[dict]:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"No existe {CONFIG_PATH}. Créalo con las cuentas antes de escanear."
        )
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    accounts = data.get("accounts", [])
    if not accounts:
        raise SystemExit(f"{CONFIG_PATH} no declara ninguna cuenta.")
    return accounts


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            log(f"AVISO: estado ilegible en {STATE_PATH}, se re-baseliniza.")
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)
    os.chmod(STATE_PATH, 0o600)


def read_password(account: dict) -> str:
    """Resuelve la contraseña de la cuenta sin exponerla nunca en la salida."""
    env_var = account.get("password_env")
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    pw_file = account.get("password_file")
    if pw_file:
        p = Path(os.path.expanduser(pw_file))
        if not p.exists():
            raise RuntimeError(
                f"falta el archivo de contraseña {p} (créalo con chmod 600)"
            )
        return p.read_text(encoding="utf-8").strip()
    raise RuntimeError(
        f"la cuenta '{account.get('id')}' no define password_file ni password_env"
    )


def classify(sender_addr: str, subject: str, headers, meta_s: str = "") -> dict:
    """Etiqueta un mensaje. La regla dura: el dinero nunca es ruido."""
    labels = set(re.findall(r"CATEGORY_[A-Z_]+", meta_s or ""))
    promo = bool({"CATEGORY_PROMOTIONS", "CATEGORY_FORUMS"} & labels)
    s_low = (subject or "").lower()
    a_low = (sender_addr or "").lower()

    financial = (any(re.search(p, a_low) for p in FINANCIAL_SENDERS)
                 or any(re.search(p, s_low) for p in FINANCIAL_SUBJECTS))
    google_sec = (any(re.search(p, a_low) for p in GOOGLE_SECURITY_SENDERS)
                  and any(re.search(p, s_low) for p in GOOGLE_SECURITY_SUBJECTS))
    third_sec = (any(re.search(p, a_low) for p in SECURITY_THIRD_PARTY)
                 and any(re.search(p, s_low) for p in SECURITY_THIRD_SUBJECTS))

    noise = False
    if promo:
        noise = True
    if any(re.search(p, a_low) for p in NOISE_SENDER_PATTERNS):
        noise = True
    if headers.get("List-Unsubscribe") or headers.get("List-Id"):
        noise = True
    if (headers.get("Precedence") or "").lower() in {"bulk", "list", "junk"}:
        noise = True
    if re.search(r"^(re:\s*)?(auto(matic)?[-\s]?reply|out of office)", s_low):
        noise = True
    if any(re.search(p, s_low) for p in NOISE_SUBJECT_PROMO):
        noise = True

    signal = any(re.search(p, s_low) for p in HIGH_SIGNAL_SUBJECT)
    if third_sec:
        signal = True
    if financial:
        signal, noise = True, False      # el dinero nunca es ruido
    if google_sec:
        noise = True                     # casi siempre es nuestro propio acceso

    return {
        "probable_noise": noise,
        "high_signal": signal,
        "self_access_hint": google_sec,
        "security_third_party": third_sec,
        "gmail_category": sorted(labels),
    }


def oauth_token(account: dict) -> str:
    """Obtiene un access token OAuth2 ejecutando el broker declarado en la cuenta."""
    cmd = (account.get("oauth") or {}).get("token_command")
    if not cmd:
        raise RuntimeError("la cuenta declara oauth pero sin token_command")
    import subprocess
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError(f"el broker OAuth falló: {(r.stderr or '').strip()[:200]}")
    tok = (r.stdout or "").strip()
    if not tok:
        raise RuntimeError("el broker OAuth devolvió un token vacío")
    return tok


def connect(account: dict):
    """Conecta por IMAP.

    OAuth2 (XOAUTH2) si la cuenta declara 'oauth' — obligatorio en cuentas
    personales de Microsoft, que deshabilitan la contraseña a nivel de protocolo.
    En el resto, contraseña de aplicación.
    """
    host = account["host"]
    port = int(account.get("port", 993))
    ctx = imaplib.IMAP4_SSL(host, port)
    if account.get("oauth"):
        token = oauth_token(account)
        # Cadena SASL de XOAUTH2. Nunca se imprime ni se registra.
        auth = f"user={account['email']}\x01auth=Bearer {token}\x01\x01"
        ctx.authenticate("XOAUTH2", lambda _challenge: auth.encode())
    else:
        ctx.login(account["email"], read_password(account))
    return ctx


def scan_folder(ctx, folder: str, last_uid: int, since_dt, max_n: int,
                is_gmail: bool) -> tuple[list, int]:
    """Devuelve (mensajes nuevos, mayor_uid_visto)."""
    typ, _ = ctx.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        raise RuntimeError(f"no se pudo abrir la carpeta {folder}")

    # Línea base: todo lo anterior al último UID ya procesado se ignora.
    if last_uid > 0:
        criteria = f"UID {last_uid + 1}:*"
    else:
        criteria = f'(SINCE "{since_dt.strftime("%d-%b-%Y")}")'

    typ, data = ctx.uid("SEARCH", None, criteria)
    if typ != "OK":
        raise RuntimeError(f"SEARCH falló en {folder}")

    uids = [int(u) for u in (data[0] or b"").split()]
    uids = [u for u in uids if u > last_uid]
    uids = uids[:max_n]
    max_seen = last_uid

    out = []
    if uids:
        uid_set = ",".join(str(u) for u in uids)
        items = ("(FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT "
                 "DATE MESSAGE-ID LIST-UNSUBSCRIBE LIST-ID PRECEDENCE "
                 "AUTO-SUBMITTED)]")
        if is_gmail:
            items += " X-GM-LABELS"
        items += ")"
        typ, fetched = ctx.uid("FETCH", uid_set, items)
        if typ != "OK":
            raise RuntimeError(f"FETCH falló en {folder}")
        # OJO con el formato de FETCH: Gmail pone "UID nnn" DENTRO de los
        # metadatos de la tupla, pero Outlook/Exchange lo pone DESPUÉS del
        # literal del cuerpo, como fragmento suelto ("... UID 107110)"). Si
        # solo se mira item[0], en Outlook no se encuentra ningún UID y se
        # descartan todos los mensajes en silencio (0 nuevos con el INBOX
        # lleno). Se reconstruye el bloque de metadatos concatenando la parte
        # previa a la tupla con los fragmentos sueltos que la siguen.
        records: list[tuple[str, bytes]] = []
        cur_meta = None
        cur_raw = b""
        for item in fetched:
            if isinstance(item, tuple) and len(item) >= 2:
                if cur_meta is not None:
                    records.append((cur_meta, cur_raw))
                first, body = item[0], item[1]
                cur_meta = (first.decode("utf-8", "replace")
                            if isinstance(first, bytes) else str(first))
                cur_raw = body if isinstance(body, bytes) else str(body).encode()
            elif isinstance(item, bytes):
                if cur_meta is None:
                    continue
                cur_meta += " " + item.decode("utf-8", "replace")
        if cur_meta is not None:
            records.append((cur_meta, cur_raw))

        for meta_s, raw in records:
            m = re.search(r"UID (\d+)", meta_s)
            uid = int(m.group(1)) if m else None
            if uid is None:
                continue
            max_seen = max(max_seen, uid)
            flags = re.findall(r"\\[A-Za-z]+", meta_s)
            try:
                msg = email.message_from_bytes(raw)
            except Exception:
                continue
            frm_name, frm_addr = parse_addr(msg.get("From", ""))
            subj = decode_header_value(msg.get("Subject"))
            date_hdr = msg.get("Date", "")
            try:
                dt = email.utils.parsedate_to_datetime(date_hdr)
                if dt and dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                dt = None
            rec = {
                "uid": uid,
                "folder": folder,
                "from_name": frm_name,
                "from_addr": frm_addr,
                "to": decode_header_value(msg.get("To")),
                "subject": subj,
                "date": dt.isoformat() if dt else date_hdr,
                "date_epoch": int(dt.timestamp()) if dt else None,
                "unread": (not any("Seen" in f for f in flags)) if flags else None,
                "message_id": (msg.get("Message-ID") or "").strip()[:200],
            }
            rec.update(classify(frm_addr, subj, msg, meta_s))
            out.append(rec)
    return out, max_seen


def read_message(account: dict, folder: str, uid: int, limit: int = 4000) -> dict:
    """Lee un mensaje concreto: permite verificar un aviso antes de actuar."""
    ctx = connect(account)
    try:
        ctx.select(f'"{folder}"', readonly=True)
        typ, data = ctx.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK":
            raise RuntimeError(f"no se pudo leer el uid {uid}")
        raw = None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                raw = item[1]
        if raw is None:
            raise RuntimeError("respuesta vacía del servidor")
        msg = email.message_from_bytes(raw)
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    body = (part.get_payload(decode=True) or b"").decode(
                        part.get_content_charset() or "utf-8", errors="replace")
                    break
            if not body:
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        html = (part.get_payload(decode=True) or b"").decode(
                            part.get_content_charset() or "utf-8", errors="replace")
                        body = re.sub(r"<[^>]+>", " ", html)
                        break
        else:
            body = (msg.get_payload(decode=True) or b"").decode(
                msg.get_content_charset() or "utf-8", errors="replace")
        body = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", body)).strip()
        return {
            "uid": uid, "folder": folder,
            "from": decode_header_value(msg.get("From")),
            "to": decode_header_value(msg.get("To")),
            "subject": decode_header_value(msg.get("Subject")),
            "date": msg.get("Date"),
            "body": body[:limit],
            "truncated": len(body) > limit,
        }
    finally:
        try:
            ctx.logout()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Escáner IMAP incremental de Hermes.")
    ap.add_argument("--since-hours", type=int, default=24,
                    help="ventana de tiempo solo para el primer escaneo de una carpeta")
    ap.add_argument("--max-per-folder", type=int, default=60)
    ap.add_argument("--account", default=None, help="filtra por id o email")
    ap.add_argument("--reset", action="store_true", help="olvida el estado previo")
    ap.add_argument("--check", action="store_true", help="solo valida credenciales")
    ap.add_argument("--read", type=int, default=None, help="lee un mensaje por UID")
    ap.add_argument("--folder", default="INBOX", help="carpeta para --read")
    args = ap.parse_args()

    accounts = load_config()
    if args.account:
        needle = args.account.lower()
        accounts = [a for a in accounts
                    if needle in (a.get("id", "").lower(), a.get("email", "").lower())]
        if not accounts:
            raise SystemExit(f"Ninguna cuenta coincide con '{args.account}'.")

    # --- modo lectura de un mensaje concreto ---
    if args.read is not None:
        try:
            res = read_message(accounts[0], args.folder, args.read)
        except Exception as e:
            raise SystemExit(f"no se pudo leer: {type(e).__name__}: {e}")
        json.dump(res, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    state = {} if args.reset else load_state()
    since_dt = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
    result = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "window_hours": args.since_hours, "accounts": [], "errors": []}

    for acc in accounts:
        aid = acc.get("id") or acc.get("email")
        is_gmail = "gmail" in (str(acc.get("provider", "")) + str(acc.get("host", ""))).lower()
        folders = acc.get("folders") or DEFAULT_FOLDERS
        acc_out = {"id": aid, "email": acc.get("email"),
                   "provider": acc.get("provider", ""), "messages": [], "folders": {}}
        try:
            ctx = connect(acc)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            log(f"ERROR de conexión en {aid}: {err}")
            result["errors"].append({"account": aid, "stage": "login", "error": err})
            acc_out["status"] = "login_failed"
            result["accounts"].append(acc_out)
            continue

        acc_out["status"] = "ok"
        try:
            for folder in folders:
                key = f"{aid}|{folder}"
                last = int(state.get(key, {}).get("last_uid", 0))
                try:
                    msgs, max_seen = scan_folder(ctx, folder, last, since_dt,
                                                 args.max_per_folder, is_gmail)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    log(f"ERROR en {aid}/{folder}: {err}")
                    result["errors"].append({"account": aid, "stage": f"folder:{folder}",
                                             "error": err})
                    continue
                acc_out["messages"].extend(msgs)
                acc_out["folders"][folder] = {"new": len(msgs), "last_uid": max_seen}
                if not args.check:
                    state[key] = {
                        "last_uid": max_seen,
                        "folder": folder,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
        finally:
            try:
                ctx.logout()
            except Exception:
                pass

        acc_out["new_total"] = len(acc_out["messages"])
        acc_out["noise_count"] = sum(1 for m in acc_out["messages"] if m["probable_noise"])
        acc_out["high_signal_count"] = sum(1 for m in acc_out["messages"]
                                           if m["high_signal"])
        acc_out["self_access_count"] = sum(1 for m in acc_out["messages"]
                                           if m.get("self_access_hint"))
        result["accounts"].append(acc_out)

    if not args.check:
        save_state(state)

    result["totals"] = {
        "new": sum(a.get("new_total", 0) for a in result["accounts"]),
        "high_signal": sum(a.get("high_signal_count", 0) for a in result["accounts"]),
        "noise": sum(a.get("noise_count", 0) for a in result["accounts"]),
        "self_access": sum(a.get("self_access_count", 0) for a in result["accounts"]),
        "errors": len(result["errors"]),
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())