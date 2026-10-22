# CLAUDE.md

Guía para trabajar en este repositorio con Claude Code.

## Resumen en una línea

Scripts de Python (solo biblioteca estándar) que operan buzones por **IMAP** para
inventariar, agrupar en carpetas, purgar a la papelera y avisar de lo importante.

## Reglas que no se rompen nunca

1. **Cero dependencias externas.** Nada de `pip install`, `requests`, ni
   frameworks. Solo `imaplib`, `ssl`, `email`, `json`, `urllib`. Si tu solución
   necesita una librería nueva, busca otra solución.
2. **Simulacro por defecto.** `mail_organize.py` y `mail_purge.py` no tocan el
   buzón sin `--apply`. Nunca inviertas esta lógica.
3. **Nunca borrar en firme.** La purga *mueve a la papelera*. No añadas
   `EXPUNGE` destructivo. El único `EXPUNGE` admisible es el emparejado con
   `UID EXPUNGE` sobre UIDs ya movidos, como respaldo cuando el servidor no
   soporta `MOVE`.
4. **Nunca imprimir secretos.** Ni contraseñas, ni tokens, ni su longitud, ni
   fragmentos. Los diagnósticos dicen "login OK" o "AUTHENTICATIONFAILED",
   jamás muestran el valor. Esta regla ya se violó una vez y hubo que limpiar el
   historial de la sesión.
5. **Verificar contra el servidor.** Tras un `--apply`, vuelve a contar los
   mensajes reales y compáralos con lo previsto. La salida del script no prueba
   nada por sí sola.

## Comandos frecuentes

```bash
# Comprobar que las cuentas conectan (no modifica nada)
python3 src/mail_scan.py --check

# Inventariar: carpetas y remitentes más frecuentes
python3 src/mail_inventory.py
python3 src/mail_inventory.py --senders 2000

# Agrupar (simulacro primero, siempre)
python3 src/mail_organize.py                    # simulacro de todas las cuentas
python3 src/mail_organize.py --account outlook  # una sola cuenta
python3 src/mail_organize.py --apply            # ejecutar de verdad

# Purgar a la papelera
python3 src/mail_purge.py --plan ejemplo-publicidad           # simulacro
python3 src/mail_purge.py --plan ejemplo-publicidad --apply   # ejecutar

# Organización desatendida (la que usa el planificador)
MIN_AGE_HOURS=12 src/mail_cycle.sh
```

`mail_cycle.sh` llama a `mail_organize.py --apply --min-age-hours 12 --quiet`:
mueve lo que ya tiene antigüedad y deja en el INBOX lo reciente. El margen de
12 h existe para que el vigilante (que revisa a las 8:00, 13:00 y 19:00) siempre
vea un correo antes de que sea movido de carpeta.

## Configuración

Todo vive fuera del repositorio, bajo `$HERMES_HOME` (por defecto `~/.hermes`):

| Archivo | Contenido |
|---|---|
| `secrets/mail_accounts.json` | Cuentas y **punteros** a credenciales (600) |
| `rules/mail_rules.json` | Reglas de agrupación, organizadas por perfiles |
| `rules/mail_purge.json` | Planes de purga |
| `state/mail_scan_state.json` | Último UID visto por cuenta y carpeta |

Las plantillas están en `config/*.example.json`. Si añades una opción nueva,
actualiza también el ejemplo y el README.

Instalación en una instancia nueva:

```bash
./install.sh --prefix ~/.hermes          # interactivo
./install.sh --dry-run                   # solo muestra lo que haría
./install.sh --with-cron                 # además imprime las líneas de cron
```

## Trampas que ya costaron un bug (no las reintroduzcas)

- **Los UIDs son únicos por carpeta, no por buzón.** Deduplicar UIDs entre
  carpetas distintas borra mensajes legítimos. Síntoma observado: un simulacro
  reportaba 22 mensajes donde el inventario decía 187.
- **El `FETCH` de Outlook coloca el UID fuera de los metadatos.** En Gmail el
  UID viaja dentro de la tupla de metadatos; en Outlook viene como un elemento
  suelto *después* del literal del cuerpo. Un `re.match(r'UID (\d+)')` sobre
  los metadatos funciona en Gmail y devuelve vacío en Outlook — que fue
  exactamente el bug que dejó la cuenta de Outlook en "0 nuevos" con 779
  mensajes en el INBOX. El parseo actual ya contempla ambos formatos.
- **Nombres de carpeta en IMAP modified UTF-7.** `Créditos` viaja como
  `Cr&AOk-ditos`. Usa `utf7_encode()` / `utf7_decode()` de `mail_organize.py`;
  no construyas el nombre a mano.
- **La papelera no se llama igual en todas las cuentas.** Gmail en español usa
  `[Gmail]/Papelera`; Outlook/Exchange, `Deleted`. `mail_purge.py` la
  autodetecta por el atributo `\Trash` del servidor (`find_trash()`); no
  vuelvas a fijar el nombre en duro.
- **`mail_rules.json` funciona por perfiles.** Una regla sin perfil asignado no
  se ejecuta, o peor: se aplica a la cuenta equivocada y crea carpetas basura.
- **El escáner se autoalimenta en Gmail.** Cada conexión nueva puede generar una
  "Alerta de seguridad" de Google que cae en el propio INBOX y parece señal
  alta. Están marcadas con `self_access_hint`.
- **Microsoft personal no acepta contraseña en IMAP.** El servidor solo anuncia
  `AUTH=XOAUTH2 LOGINDISABLED`. La cuenta se resuelve con `oauth.token_command`
  apuntando a `ms_oauth.py token`. La ruta del refresh token se puede
  sobreescribir con `HERMES_MS_TOKEN` si hay varias cuentas de Microsoft.

## Estilo

- Python 3.10+, `from __future__ import annotations`, anotaciones modernas
  (`str | None`).
- Mensajes de usuario e identificadores **en español**, por coherencia con el
  resto del repositorio.
- Salida de datos en JSON por `stdout`; mensajes para humanos por `stderr`.
- Antes de dar por bueno un cambio con efectos: ejecuta el simulacro,
  comprueba que los números cuadran con el inventario, y solo entonces aplica.
