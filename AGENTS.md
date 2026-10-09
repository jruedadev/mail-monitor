# AGENTS.md

Contexto para agentes de código que trabajen en este repositorio.

## Qué es esto

Un conjunto de scripts de Python que operan buzones de correo por **IMAP** para
tres fines: inventariar, agrupar por reglas y purgar a la papelera. Se ejecutan
de forma autónoma (típicamente desde un planificador), no como cliente
interactivo.

**Restricción de partida:** cero dependencias externas. Solo la biblioteca
estándar de Python (`imaplib`, `ssl`, `email`, `json`, `urllib`). No introduzcas
`pip install`, ni `requests`, ni frameworks de terceros: el valor del proyecto
es que se ejecuta en cualquier máquina con Python sin preparar nada.

## Los archivos

| Archivo | Responsabilidad |
|---|---|
| `src/mail_scan.py` | Núcleo. Escaneo incremental por UID, clasificación heurística, conexión IMAP (contraseña **u** OAuth2), lectura de un mensaje concreto. Los demás scripts importan de aquí. |
| `src/mail_inventory.py` | Solo lectura. Dimensiona un buzón: carpetas con conteos, top de remitentes y dominios. |
| `src/mail_organize.py` | Motor de reglas. Incluye la traducción de nombres a IMAP modified UTF-7 y el reparto de reglas por perfil de cuenta. |
| `src/mail_purge.py` | Mueve correo a la papelera según un plan. |
| `src/ms_oauth.py` | Broker OAuth2 (flujo de código de dispositivo) para cuentas personales de Microsoft. |

`mail_scan.py` es el único que no se puede renombrar sin tocar el resto: los
otros hacen `sys.path.insert(0, Path(__file__).parent)` e `import mail_scan`.

## Configuración

Nada de credenciales en el repositorio. Todo vive bajo `$HERMES_HOME`
(por defecto `~/.hermes`), **fuera del árbol del proyecto**:

```
$HERMES_HOME/secrets/mail_accounts.json   # cuentas y punteros a credenciales
$HERMES_HOME/rules/mail_rules.json        # reglas de agrupación, por perfiles
$HERMES_HOME/rules/mail_purge.json        # planes de purga
$HERMES_HOME/state/mail_scan_state.json   # último UID visto por cuenta+carpeta
```

Las plantillas están en `config/*.example.json`. Al añadir una opción de
configuración nueva, **añádela también al ejemplo** y documéntala en el README.

`HERMES_HOME` viene de Hermes Agent, pero el proyecto no depende de él: es solo
una variable de entorno con una ruta.

## Invariantes que no se rompen

1. **Simulacro por defecto.** `mail_organize.py` y `mail_purge.py` no modifican
   nada sin `--apply` explícito. Cualquier función nueva con efectos debe
   respetarlo.
2. **Nunca borrar de forma definitiva.** La purga *mueve a la papelera*. No
   añadas `EXPUNGE` destructivo ni borrado de carpetas. El `EXPUNGE` que existe
   en `move_uids` va emparejado con `UID EXPUNGE` (solo los UIDs movidos) y solo
   como camino alternativo cuando el servidor no soporta `MOVE`.
3. **Nunca imprimir un valor derivado de una credencial.** Ni la contraseña, ni
   el token, ni "la forma", ni un `re.findall` sobre su contenido. Los
   diagnósticos reportan booleanos ("login OK", "AUTHENTICATIONFAILED"), jamás
   fragmentos del secreto ni longitudes que revelen su estructura.
4. **Verificar contra el servidor.** Tras un `--apply`, leer los conteos reales
   del buzón y contrastarlos con los previstos. La salida del propio script no
   es prueba de nada.
5. **Los destinos inexistentes se omiten** salvo `create: true`. Una regla mal
   escrita debe fallar de forma visible y no destructiva.

## Trampas conocidas (ya costaron un bug cada una)

- **Los UIDs son únicos por carpeta, no por buzón.** Deduplicar UIDs entre
  carpetas distintas descarta mensajes legítimos. Síntoma real que se observó:
  un simulacro reportaba 22 mensajes donde el inventario decía 187, porque los
  números de la carpeta anterior "reclamaban" los de la siguiente.
- **Un simulacro que no cuadra con el inventario es un bug del simulacro**, no
  del servidor. Contrástalo con `EXISTS` tras `SELECT`, `STATUS MESSAGES` y
  `UID SEARCH ALL`: los tres deben dar el mismo número antes de mover nada.
- **Nombres de carpeta en IMAP modified UTF-7** (RFC 3501): `Créditos` viaja
  como `Cr&AOk-ditos`, `Telefonía` como `Telefon&AO0-a`. `LIST` los devuelve
  codificados; hay que usarlos así al hablar con el servidor y decodificarlos
  solo para mostrarlos.
- **`X-GM-LABELS` puede venir vacío** según la cuenta, así que no construyas la
  detección de promociones sobre `CATEGORY_PROMOTIONS`; úsala solo como
  refuerzo.
- **El escáner se autoalimenta en Gmail.** Cada conexión nueva puede generar un
  correo "Alerta de seguridad" de Google que cae en el propio INBOX y el
  vigilante reporta como señal alta. Están marcadas con `self_access_hint` y se
  agrupan en una línea.
- **`mail_rules.json` es por perfiles.** Las reglas de un perfil no se aplican a
  las cuentas de otro. Si añades reglas sin asignarlas a un perfil, no se
  ejecutan (o peor: se aplican a la cuenta equivocada y crean carpetas basura).

## Autenticación: lo que hay que saber antes de tocar `connect()`

- Gmail: contraseña de aplicación en un archivo `600`. `auth.type` = login normal.
- Microsoft personal: **la contraseña no existe como mecanismo**. El servidor
  anuncia `AUTH=XOAUTH2 LOGINDISABLED`, sin `AUTH=PLAIN`. Se resuelve con
  `oauth.token_command` en la cuenta, que debe imprimir un access token en
  stdout. `connect()` detecta la clave `oauth` y usa `AUTHENTICATE XOAUTH2`; el
  token se pide fresco en cada conexión porque caduca en torno a una hora.
- El `client_id` público que usa `ms_oauth.py` evita registrar una app en Azure.
  Es un atajo, no una concesión del proveedor. Si dejara de funcionar, la salida
  es registrar una aplicación propia.

## Estilo

- Python 3.10+, con `from __future__ import annotations` y anotaciones de tipo
  modernas (`str | None`).
- Mensajes de usuario en **español** (los usuarios de este proyecto lo son);
  identificadores y comentarios de código, también en español por coherencia con
  el resto del repositorio.
- Los errores se reportan por `stderr` y se acumulan en la clave `errors` del
  JSON de salida; el código de salida es distinto de cero solo cuando el fallo
  impide completar la tarea.
- Salida en JSON por `stdout` para que otro programa la consuma; los mensajes
  para humanos, por `stderr`.
