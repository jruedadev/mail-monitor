#!/usr/bin/env python3
"""
Broker OAuth2 para la cuenta personal de Microsoft de Johan.

Microsoft deshabilitó la autenticación por contraseña en IMAP (el servidor solo
anuncia AUTH=XOAUTH2), así que la única vía es un token OAuth2. Este script
obtiene y renueva ese token sin dependencias externas.

Se usa el client_id público de Thunderbird para cuentas personales, que evita
registrar una aplicación propia en Azure. Es el mismo apaño que usan las
herramientas libres; si Microsoft lo cortara, habría que registrar una app.

Subcomandos:
    python3 ms_oauth.py start     # pide el código de dispositivo (caduca en ~15 min)
    python3 ms_oauth.py finish    # sondea hasta obtener el token (tras el login)
    python3 ms_oauth.py token     # imprime un access token fresco (lo usa mail_scan)
    python3 ms_oauth.py status    # ¿hay refresh token guardado?
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
SECRETS = HERMES_HOME / "secrets"
STATE_PATH = SECRETS / "ms_device_code.json"
TOKEN_PATH = SECRETS / "ms_refresh_outlook.json"

CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"   # Thunderbird, cuentas personales
TENANT = "consumers"
SCOPE = ("https://outlook.office.com/IMAP.AccessAsUser.All "
         "https://outlook.office.com/SMTP.Send offline_access")
BASE = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"


def _post(url: str, fields: dict, timeout: int = 25) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _save(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    tmp.replace(path)
    os.chmod(path, 0o600)


def cmd_start() -> int:
    try:
        r = _post(f"{BASE}/devicecode", {"client_id": CLIENT_ID, "scope": SCOPE})
    except urllib.error.HTTPError as e:
        print("No se pudo iniciar el flujo:", e.code, e.read().decode()[:300])
        return 1
    _save(STATE_PATH, {
        "device_code": r["device_code"],
        "interval": int(r.get("interval", 5)),
        "expires_at": time.time() + int(r.get("expires_in", 900)),
    })
    print("=" * 62)
    print("  Abre esta URL en el navegador:")
    print(f"    {r.get('verification_uri') or r.get('verification_uri_complete')}")
    print()
    print("  Y escribe este código:")
    print(f"    {r.get('user_code')}")
    print("=" * 62)
    print(f"  Caduca en {int(r.get('expires_in', 900)) // 60} minutos.")
    print("  Inicia sesión con usuario@outlook.com y acepta los permisos.")
    return 0


def cmd_finish() -> int:
    if not STATE_PATH.exists():
        print("No hay un código pendiente. Ejecuta primero 'start'.")
        return 1
    st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if time.time() > st.get("expires_at", 0):
        print("El código caducó. Vuelve a ejecutar 'start'.")
        return 1
    interval = max(3, int(st.get("interval", 5)))
    print("Esperando a que completes el login en el navegador...")
    deadline = st.get("expires_at", time.time() + 900)
    while time.time() < deadline:
        try:
            r = _post(f"{BASE}/token", {
                "client_id": CLIENT_ID,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": st["device_code"],
            })
            _save(TOKEN_PATH, {
                "refresh_token": r["refresh_token"],
                "scope": r.get("scope", SCOPE),
                "obtained_at": time.time(),
            })
            STATE_PATH.unlink(missing_ok=True)
            print("LISTO: refresh token guardado. La cuenta ya es accesible.")
            return 0
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                err = json.loads(body).get("error", "")
            except Exception:
                err = body[:120]
            if err == "authorization_pending":
                time.sleep(interval)
                continue
            if err in ("slow_down",):
                interval += 5
                time.sleep(interval)
                continue
            print("Error de autenticación:", err or body[:200])
            return 1
    print("Se agotó el tiempo esperando el login.")
    return 1


def cmd_token() -> int:
    if not TOKEN_PATH.exists():
        print("No hay refresh token. Ejecuta 'start' y 'finish' primero.", file=sys.stderr)
        return 1
    rt = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))["refresh_token"]
    try:
        r = _post(f"{BASE}/token", {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "scope": SCOPE,
        })
    except urllib.error.HTTPError as e:
        print("No se pudo renovar el token:", e.code, e.read().decode()[:300], file=sys.stderr)
        return 1
    if "refresh_token" in r:
        _save(TOKEN_PATH, {"refresh_token": r["refresh_token"],
                           "scope": r.get("scope", SCOPE), "obtained_at": time.time()})
    sys.stdout.write(r["access_token"])
    return 0


def cmd_status() -> int:
    ok = TOKEN_PATH.exists()
    print("refresh token:", "guardado" if ok else "NO")
    if ok:
        p = TOKEN_PATH.stat()
        print(f"  archivo: {TOKEN_PATH} (permisos {oct(p.st_mode)[-3:]})")
    if STATE_PATH.exists():
        st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        print("hay un código de dispositivo pendiente, caduca en "
              f"{int(st.get('expires_at', 0) - time.time())}s")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    sys.exit({"start": cmd_start, "finish": cmd_finish,
              "token": cmd_token, "status": cmd_status}.get(cmd, cmd_status)())
