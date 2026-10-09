# mail-monitor

Asistente de correo por IMAP, sin dependencias externas: **inventaría** un buzón, **clasifica** los mensajes en carpetas por reglas, **purga** a la papelera lo que ya no sirve y **avisa** de lo importante.

Escrito en Python usando solo la biblioteca estándar (`imaplib` + `ssl`). No necesita cliente de correo, ni servidor intermedio, ni `sudo`.

## Qué hace cada script

| Script | Función |
|---|---|
| `mail_scan.py` | Escaneo **incremental**: recuerda el último UID leído por carpeta y devuelve solo lo nuevo, en JSON. Clasifica el ruido evidente (boletines, promociones) y marca la señal alta (dinero, seguridad, trabajo). |
| `mail_inventory.py` | Inventario: carpetas con su número de mensajes, y los remitentes/dominios más frecuentes de un buzón. Sirve para dimensionar la acumulación **antes** de diseñar reglas. |
| `mail_organize.py` | Aplica reglas de agrupación: mueve correo del INBOX a las carpetas que definas. **Simula por defecto.** |
| `mail_purge.py` | Mueve correo a la papelera según un plan. **Simula por defecto** y **nunca borra de forma definitiva**. |
| `ms_oauth.py` | Broker OAuth2 para cuentas personales de Microsoft, obligado porque Outlook deshabilitó la contraseña en IMAP. |

## Requisitos

- Python 3.10 o superior.
- Una cuenta de correo con IMAP habilitado.

Nada más. Sin `pip install`, sin compilación.

## Instalación

```bash
git clone https://github.com/jruedadev/mail-monitor.git
cd mail-monitor
```

## Configuración

Los scripts leen tres archivos bajo `$HERMES_HOME` (por defecto `~/.hermes`; puedes apuntarla donde quieras con esa variable de entorno):

```
$HERMES_HOME/
├── secrets/mail_accounts.json     # las cuentas (permisos 600)
├── rules/mail_rules.json          # reglas de agrupación
└── rules/mail_purge.json          # plan de purga
```

En `config/` tienes un ejemplo de cada uno. Cópialos y edítalos:

```bash
mkdir -p ~/.hermes/secrets ~/.hermes/rules
chmod 700 ~/.hermes/secrets
cp config/mail_accounts.example.json ~/.hermes/secrets/mail_accounts.json
cp config/mail_rules.example.json    ~/.hermes/rules/mail_rules.json
cp config/mail_purge.example.json    ~/.hermes/rules/mail_purge.json
chmod 600 ~/.hermes/secrets/mail_accounts.json
```

## Autenticación

### Gmail — contraseña de aplicación

Gmail rechaza la contraseña normal de la cuenta por IMAP. Hay que generar una **contraseña de aplicación**, lo que exige tener activada la verificación en dos pasos.

Guárdala en un archivo con permisos `600` **sin que pase por el chat ni por el historial de shell**:

```bash
read -rsp 'App password: ' P && printf '%s' "$P" > ~/.hermes/secrets/apppass_gmail_personal \
  && chmod 600 ~/.hermes/secrets/apppass_gmail_personal && unset P
```

### Microsoft personal (outlook.com, hotmail.com, live.com) — OAuth2 obligatorio

**No existe la opción de contraseña.** Desde septiembre de 2024 el servidor anuncia solo `AUTH=XOAUTH2` y `LOGINDISABLED`: la contraseña está deshabilitada en el propio protocolo, así que una contraseña de aplicación tampoco sirve. Comprobable:

```bash
openssl s_client -connect outlook.office365.com:993 -servername outlook.office365.com
# AUTH=XOAUTH2 LOGINDISABLED ... (sin AUTH=PLAIN)
```

`ms_oauth.py` implementa el flujo de **código de dispositivo** para obtener el token:

```bash
python3 src/ms_oauth.py start    # muestra una URL y un código (caduca en ~15 min)
python3 src/ms_oauth.py finish   # espera a que completes el login y guarda el refresh token
python3 src/ms_oauth.py token    # imprime un access token fresco (lo usa mail_scan.py)
python3 src/ms_oauth.py status   # ¿hay refresh token guardado?
```

Nota sobre el `client_id`: se usa uno público para no obligar a registrar una aplicación propia en Azure. Funciona, pero no es una concesión hecha a ti — si el proveedor lo cortara, la alternativa es registrar una app propia y cambiar una constante en `ms_oauth.py`.

## Uso

### 1. Inventariar antes de decidir

```bash
python3 src/mail_inventory.py                          # carpetas de todas las cuentas
python3 src/mail_inventory.py --account gmail-personal
python3 src/mail_inventory.py --senders 2000           # top de remitentes del INBOX
```

### 2. Comprobar que las conexiones funcionan

```bash
python3 src/mail_scan.py --check
```

### 3. Agrupar (simulacro primero, siempre)

```bash
python3 src/mail_organize.py                           # simulacro: no toca nada
python3 src/mail_organize.py --account outlook
python3 src/mail_organize.py --rule promo-tienda       # una sola regla
python3 src/mail_organize.py --apply                   # ejecutar de verdad
```

### 4. Purgar a la papelera

```bash
python3 src/mail_purge.py --plan ejemplo-publicidad              # simulacro
python3 src/mail_purge.py --plan ejemplo-publicidad --apply
```

### 5. Vigilar y avisar (opcional)

`mail_scan.py` está pensado para ejecutarse desde un planificador y pasar su JSON a un agente o script que decida qué avisar. Un cron cada pocas horas:

```cron
0 8,13,19 * * * cd /ruta/mail-monitor && HERMES_HOME=$HOME/.hermes python3 src/mail_scan.py
```

## Decisiones de diseño (por qué es seguro)

- **Simulacro por defecto.** `mail_organize.py` y `mail_purge.py` no modifican nada sin `--apply`.
- **La purga mueve, no borra.** El destino es la papelera del proveedor, que el usuario puede auditar y recuperar. El código nunca hace `EXPUNGE` destructivo.
- **Los destinos inexistentes se omiten.** Una regla cuyo destino no exista no crea nada salvo que lleve `create: true` explícito.
- **Los UIDs son únicos por carpeta, no por buzón.** Cualquier lógica que procese varias carpetas en una pasada debe tenerlo en cuenta; deduplicar entre carpetas distintas descarta mensajes legítimos.
- **Nombres de carpeta en IMAP modified UTF-7.** El servidor devuelve `Cr&AOk-ditos` para `Créditos`. Las reglas se escriben legibles y el motor traduce en ambos sentidos.
- **Verificar contra el servidor, no contra el script.** Después de cada `--apply`, comparar los conteos reales con los previstos. Un simulacro que no cuadra con el inventario es un bug del simulacro.

## Estructura

```
mail-monitor/
├── src/
│   ├── mail_scan.py        # escaneo incremental + clasificación + broker OAuth
│   ├── mail_inventory.py   # inventario de carpetas y remitentes
│   ├── mail_organize.py    # motor de reglas (UTF-7, perfiles por cuenta)
│   ├── mail_purge.py       # purga a la papelera
│   └── ms_oauth.py         # OAuth2 para Microsoft personal
├── config/
│   ├── mail_accounts.example.json
│   ├── mail_rules.example.json
│   └── mail_purge.example.json
├── AGENTS.md               # contexto del proyecto para agentes de código
└── LICENSE
```

## Licencia

MIT. Ver [LICENSE](LICENSE).
