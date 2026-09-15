# API de Reconocimiento de Placas Vehiculares (ALPR)

Backend FastAPI: le mandas la foto de un vehículo y te devuelve **el texto de
la placa y la foto recortada de la placa**. `/detect` no guarda nada (ni
imágenes ni historial), así que corre en cualquier contenedor sin disco
persistente.

La detección y el OCR corren con modelos ONNX locales: sin API key, sin
tokens, sin cuota. Lo externo es opcional y viene apagado: un fallback a Groq
y la vigilancia de una carpeta de iCloud Drive, que guarda sus lecturas en
Postgres.

## La idea central: son dos problemas, no uno

```
   foto            DETECCIÓN                   OCR
  ┌─────┐      ┌──────────────┐        ┌────────────────┐
  │ 🚗  │ ───► │   YOLOv9     │ ─────► │  CCT (solo     │ ───► "CVL65718"
  └─────┘      │ ¿DÓNDE está  │ recorte│  placas)       │      + recorte
               │  la placa?   │        │ ¿QUÉ dice?     │      + confianza
               └──────────────┘        └────────────────┘
```

Mandarle la foto completa a un modelo de lenguaje multimodal falla por una
razón concreta: esos servicios comprimen **cada imagen a ~2,048 tokens** sin
importar su resolución. Si la placa ocupa el 1 % de la foto, le tocan ~20
tokens y el modelo adivina. Detectando primero y recortando, todo el
presupuesto de detalle se gasta en la placa.

Esta separación también es lo que te da un número honesto: el OCR devuelve la
confianza **de cada carácter**, así que sabes cuándo no confiar en la lectura.

---

## Uso

`POST /detect` con la imagen como `multipart/form-data`, en el campo `archivo`:

```bash
curl -X POST https://TU-SERVICIO.onrender.com/detect \
  -H "X-API-Key: TU_API_KEY" \
  -F "archivo=@carro.jpg"
```

Respuesta:

```json
{
  "placas": [
    {
      "texto": "CVL65718",
      "propietario": null,
      "imagen_base64": "/9j/4AAQSkZJRgABAQAAAQABAAD...",
      "fuente": "local",
      "confianza_deteccion": 0.934,
      "confianza_ocr": 0.9999,
      "region": "Unknown",
      "bbox": { "x1": 245, "y1": 313, "x2": 395, "y2": 364 },
      "texto_local": "CVL65718",
      "texto_groq": null,
      "nota_fallback": null
    }
  ],
  "ms_procesamiento": 57.38
}
```

- **`placas`** viene vacía si la foto no tiene ninguna placa. Si hay varias, van
  ordenadas de más a menos confianza del detector: `placas[0]` es la más clara.
- **`texto`** es la placa en mayúsculas, solo `A-Z` y `0-9`. Puede ser `null` si
  el detector encontró la placa pero el OCR no pudo leer nada.
- **`imagen_base64`** es el recorte de la placa en JPEG.
- **`propietario`** trae los datos del dueño si la placa está registrada (ver
  [Dueños de las placas](#dueños-de-las-placas)); si no, `null`.
- **`confianza_ocr`** es la del carácter **más débil**, no el promedio (ver
  [Configuración](#configuración)).

Desde JavaScript:

```js
const form = new FormData();
form.append("archivo", input.files[0]);

const r = await fetch("https://TU-SERVICIO.onrender.com/detect", {
  method: "POST",
  headers: { "X-API-Key": "TU_API_KEY" },
  body: form,
});
const { placas } = await r.json();

if (placas.length) {
  textoPlaca.textContent = placas[0].texto;
  fotoPlaca.src = `data:image/jpeg;base64,${placas[0].imagen_base64}`;
}
```

### Probar desde el navegador

Abre [`probar.html`](probar.html) con doble clic: pegas la API key, eliges una
foto y te muestra el texto de la placa y el recorte ya como imagen. Swagger
(`/docs`) también sirve, pero ahí el recorte llega como texto base64.

### Errores

| Código | Cuándo |
|---|---|
| `400` | El archivo llegó vacío o no es una imagen |
| `401` | Falta el header `X-API-Key` o no coincide (solo si `API_KEY` está definida) |
| `413` | La imagen pesa más de `MAX_MB_IMAGEN` (default 10 MB) |
| `422` | No se mandó el campo `archivo` |

### Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/health` | Estado del servicio. Render lo usa como health check; no pide API key. |
| `POST` | `/detect` | Imagen → texto y recorte de cada placa |
| `GET` | `/icloud/lecturas` | Fotos recientes de la carpeta de iCloud con su placa (`?placa=HAB1234` para buscar) |
| `GET` | `/icloud/lecturas/{id}/recorte` | Recorte JPEG de la placa de una foto de iCloud |
| `POST` | `/icloud/escanear` | Revisa la carpeta y lee las fotos pendientes ya |
| `GET` | `/icloud/estado` | Sesión con Apple, fotos pendientes y último error |
| `POST` | `/icloud/sesion` | Entra a iCloud; si Apple pide verificación, te manda el código |
| `POST` | `/icloud/codigo` | Le pasa a la API el código de verificación de Apple |
| `GET` | `/propietarios` | Placas registradas con los datos de su dueño |
| `PUT` | `/propietarios/{placa}` | Registra o cambia el dueño de una placa |
| `DELETE` | `/propietarios/{placa}` | Saca una placa del registro |

Los `/icloud/*` responden `503` si la vigilancia de iCloud no está configurada
(ver [Vigilar una carpeta de iCloud Drive](#vigilar-una-carpeta-de-icloud-drive)).
Los `/propietarios` piden `ADMIN_API_KEY` en vez de `API_KEY` (ver
[Dueños de las placas](#dueños-de-las-placas)).

Swagger en `/docs`; el botón **Authorize** es para poner la API key.

---

## Correr en local

Requiere Python 3.10 o superior (probado en 3.14).

```bat
python -m venv .venv
.venv\Scripts\activate          ::  Linux/Mac:  source .venv/bin/activate
pip install -r requirements.txt
python scripts\precargar_modelos.py
uvicorn app.main:app --reload --port 8000
```

`precargar_modelos.py` descarga los pesos ONNX (~13 MB) a `~/.cache` una sola
vez; después la API arranca sin internet. Docs en <http://127.0.0.1:8000/docs>.

Para medir precisión sobre tus fotos sin pasar por HTTP:

```bat
python scripts\test_alpr.py C:\ruta\a\fotos
python scripts\test_alpr.py C:\ruta\a\fotos --recortes C:\ruta\recortes
```

Con `--recortes` guarda el recorte de cada placa: es la misma imagen que
devuelve `/detect`.

---

## Deploy en Render

El repo trae `Dockerfile` y `render.yaml`, así que el deploy es un Blueprint:

1. Sube el proyecto a GitHub. El `.gitignore` ya deja fuera `.env`, `.venv` y `data/`.
2. En Render: **New → Blueprint** y elige el repo.
3. Te va a pedir `GROQ_API_KEY` y las variables de iCloud (`ICLOUD_*` y
   `DATABASE_URL`): déjalas vacías si no las usas. `API_KEY` la genera Render
   sola; la copias desde **Environment** en el panel del servicio.
4. Cuando termine, abre `https://TU-SERVICIO.onrender.com/health`.

**Por qué Docker y no el runtime nativo de Python:** los modelos se descargan
durante el build y quedan dentro de la imagen, así el contenedor arranca sin
bajar nada. El Dockerfile corre `precargar_modelos.py`, que hace fallar el
build si los modelos no quedaron en caché: un deploy roto no llega a producción.

**Plan gratis, lo que hay que saber:**

- Se duerme tras 15 min sin tráfico, y la primera petición después tarda
  alrededor de un minuto en despertarlo. El plan Starter no se duerme.
- La CPU es compartida y chica: cada detección tarda bastante más que en tu PC
  (en local son ~60–120 ms).
- 512 MB de RAM alcanzan con los modelos default. Si subes a `yolo-v9-s-608` o
  `global-plates-mobile-vit-v2`, revisa la memoria en las métricas del servicio.
- Si cambias `DETECTOR_MODEL` u `OCR_MODEL` en las variables de Render, esos
  modelos no están en la imagen y se descargan en cada arranque. Funciona, pero
  arranca más lento. Para dejarlos fijos, cambia el default en `app/config.py`
  y el siguiente build los hornea.

### API key y CORS

- **`API_KEY`**: si está definida, `/detect` exige el header `X-API-Key`. Ojo:
  si llamas a la API desde una página web, la key queda visible en el
  navegador. Frena el abuso casual, pero no es un secreto; si necesitas que lo
  sea, llama a esta API desde tu propio backend.
- **`ADMIN_API_KEY`**: la de `/propietarios`. Esa no va en la app (ver
  [Dueños de las placas](#dueños-de-las-placas)).
- **`CORS_ORIGENES`**: qué dominios pueden llamar a la API desde un navegador,
  separados por coma (ej. `https://miapp.com`). El default `*` acepta
  cualquiera; cuando tengas el frontend publicado, pon su dominio.

---

## Vigilar una carpeta de iCloud Drive

Opcional y apagado por default. Cada foto que llega a una carpeta compartida
de iCloud Drive (en este proyecto, la que se toma cuando pasa el carro) se
registra sola, y la API lee su placa:

```
cada 2 min, o cuando la app pide lecturas:

  CloudKit: ¿qué cambió? ──▶ Postgres ("pendiente") ──▶ lee la placa ──▶ Postgres

  React Native ◀── GET /icloud/lecturas ◀── Postgres
```

Apple no tiene una API pública de iCloud Drive, así que esto funciona
distinto que con Google Drive:

- **La API entra con una cuenta de Apple** (correo y contraseña) usando
  [`pyicloud`](https://github.com/timlaing/pyicloud), una librería no
  oficial. La primera vez, y cuando Apple deja de confiar en la sesión
  (cada **unos 30 días**), hay que pasarle un código de verificación: ver
  [El código de Apple](#el-código-de-apple).
- **Apple no avisa cuando llega una foto.** La API revisa la carpeta cada
  `ICLOUD_INTERVALO_REVISION` segundos (default 120) mientras está despierta,
  y justo antes de responder `GET /icloud/lecturas`. Cada revisión le pide a
  CloudKit solo lo que cambió desde la anterior: si no hay nada nuevo, es una
  sola petición.
- **El resultado no se puede guardar en la foto**, así que va a Postgres
  junto con la sesión de Apple. Render gratis no tiene disco: sin una base
  externa habría que poner el código cada vez que el contenedor se duerme.

Por cada foto se guarda la placa más clara (`placas[0]`), su bbox y cuántas
placas había. El recorte no se guarda: `/icloud/lecturas/{id}/recorte` lo
vuelve a cortar de la foto original con el bbox, sin correr otra vez el modelo.

### Configuración

1. **Una cuenta de Apple para la API.** Mejor una nueva, solo para esto, que
   tu cuenta personal: su contraseña va a quedar en las variables de Render.
   El código de verificación le llega por SMS al teléfono de la cuenta. Si
   tiene la Protección avanzada de datos, activa *Acceder a los datos de
   iCloud en la web*. La API lee todo lo que el dueño de la carpeta le
   comparta a esa cuenta, así que no la uses para otras carpetas del mismo dueño.
2. **Con esa cuenta, agrega la carpeta a su iCloud Drive:** abre el enlace de
   la carpeta compartida, inicia sesión y toca **Agregar a iCloud Drive**.
3. **Una base Postgres gratis**, por ejemplo en [Neon](https://neon.tech):
   crea un proyecto y copia la *connection string*. La API crea sus tablas sola.
4. Variables de entorno:

   ```ini
   ICLOUD_HABILITADO=true
   ICLOUD_CARPETA=https://www.icloud.com/iclouddrive/0b4e...#Mediciones   # el enlace tal cual
   ICLOUD_APPLE_ID=cuenta-de-la-api@icloud.com
   ICLOUD_PASSWORD=...
   DATABASE_URL=postgresql://usuario:clave@host/base?sslmode=require
   ```

5. La primera vez, **pásale el código de Apple** (siguiente sección).

Al arrancar, el log dice `Vigilando la carpeta de iCloud ...` y, cuando
entra, `Sesión de iCloud abierta`.

### El código de Apple

La primera vez, y cada unos 30 días, `GET /icloud/estado` dice
`"sesion": "falta_codigo"`. Son dos pasos seguidos:

```bash
# 1. Apple manda el código por SMS (o a los dispositivos de la cuenta)
curl -X POST https://TU-SERVICIO.onrender.com/icloud/sesion -H "X-API-Key: TU_API_KEY"

# 2. Se lo pasas a la API
curl -X POST https://TU-SERVICIO.onrender.com/icloud/codigo \
  -H "X-API-Key: TU_API_KEY" -H "Content-Type: application/json" \
  -d '{"codigo": "123456"}'
```

También se puede desde `/docs`. Si el contenedor se reinicia entre los dos
pasos, el código ya no sirve: pide otro con el paso 1.

Cuando la sesión vence, la API lo intenta una vez sola y Apple te manda un
código: si te llega, pásalo directo al paso 2. Después ya no lo intenta por
su cuenta, porque cada intento es otro SMS: espera a que llames al paso 1.
Igual si Apple rechaza la contraseña (`"sesion": "rechazada"`): no reintenta
hasta que corrijas las variables (al guardar, Render reinicia la API) o
llames al paso 1, porque cada intento fallido acerca a Apple a bloquear la
cuenta.

**Desde tu PC**, sin depender de que Render siga despierto entre los dos
pasos:

```bash
python scripts/subir_sesion_icloud.py --apple-id cuenta-de-la-api@icloud.com
```

Te pide la contraseña y el código en la terminal (o reusa una sesión de
confianza que ya esté en tu PC) y guarda la sesión en Postgres. Necesita la
misma `DATABASE_URL` de Render: si no está en tu `.env`, te la pide. Después
llama `POST /icloud/sesion`.

### Cómo se mantiene al día

- **Plan gratis de Render:** el contenedor se duerme tras 15 min sin tráfico
  y, dormido, no revisa nada. La petición de la app lo despierta (~1 min) y,
  antes de responder, la API revisa la carpeta: las fotos que llegaron
  mientras dormía salen como `pendiente` y su placa se lee en segundo plano.
  Vuelve a pedir la lista en unos segundos para verlas leídas.
- Si quieres que las fotos se lean apenas llegan aunque nadie abra la app,
  mantén el servicio despierto con un *cron* gratis (ej.
  [cron-job.org](https://cron-job.org)) que llame a `GET /health` cada 10
  minutos. Un servicio despierto todo el mes cabe en las 750 horas gratis de
  Render.
- Si Render reinicia el contenedor, la API toma la sesión de Postgres y
  sigue sin pedir código.

### El campo `estado` de cada lectura

| Valor | Significado |
|---|---|
| `pendiente` | Todavía no se procesa |
| `ok` | Se encontró al menos una placa (`placa` puede ser `null` si el OCR no leyó nada) |
| `sin_placa` | El detector no encontró ninguna placa |
| `no_decodificable` | OpenCV no puede abrir el archivo. Típico: fotos **HEIC** de iPhone. Configura la cámara en "Más compatible" (JPG). |
| `muy_grande` | Pesa más de `MAX_MB_IMAGEN` |
| `error` | Falló el pipeline; el detalle va en `nota` |

Cada foto se lee una sola vez. Si la reemplazas por otra con el mismo
nombre, la API lo nota porque cambia el checksum del archivo y la vuelve a
leer. Si la borras de la carpeta, desaparece de las lecturas.

### Desde React Native

```js
const API = "https://TU-SERVICIO.onrender.com";
const headers = { "X-API-Key": API_KEY };

const { lecturas } = await (await fetch(`${API}/icloud/lecturas?limite=20`, { headers })).json();

// El recorte de la placa, con el header de la API key
<Image
  source={{ uri: `${API}/icloud/lecturas/${lecturas[0].id}/recorte`, headers }}
  style={{ width: 200, height: 60 }}
/>
```

Si alguna lectura viene `pendiente`, vuelve a pedir la lista en unos
segundos. Si la app ya usaba `/drive/lecturas`: cambia la ruta a
`/icloud/...` y `drive_id` por `id`. Lo demás de cada lectura es igual,
menos `enlace`, que ya no existe.

La API key queda dentro de la app, igual que en una página web: frena el
abuso casual pero no es un secreto.

---

## Dueños de las placas

Opcional. Registras las placas que te interesan con los datos de su dueño, y
cada vez que la API lee una de ellas (en `/detect` o en una foto de iCloud)
devuelve esos datos en `propietario`:

```json
"propietario": {
  "nombre": "Juan Pérez",
  "telefono": "9999-9999",
  "correo": null,
  "vehiculo": "Chevrolet Cavalier rojo",
  "notas": null
}
```

Se guardan en la misma base de `DATABASE_URL` (tabla `propietarios`) y se
administran con `/propietarios`, que pide **otra clave**: `ADMIN_API_KEY`. La
`API_KEY` va dentro de la app y cualquiera puede sacarla del APK; con ella
solo se ve el dueño de una placa que la API leyó, nunca la lista completa, y
no se puede cambiar nada.

1. En Render → Environment agrega `ADMIN_API_KEY` con un valor largo y
   aleatorio, por ejemplo el que da
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. En `/docs`, **Authorize** con esa clave. Sirve también para las demás rutas.
3. `PUT /propietarios/{placa}` con la placa en la ruta y los datos en el cuerpo:

   ```json
   {"nombre": "Juan Pérez", "telefono": "9999-9999", "vehiculo": "Chevrolet Cavalier rojo"}
   ```

La placa se puede escribir con espacios o guiones (`CVL 657 18`): se guarda
normalizada (`CVL65718`), igual que la lee el OCR. La comparación es exacta:
si el OCR confunde un carácter (`0`/`O`, `1`/`I`), no coincide, y así nunca
sale el dueño de otra placa.

Son datos personales: guarda solo los de personas que estén de acuerdo.

---

## El campo `fuente`: de dónde salió cada lectura

| Valor | Significado |
|-------|-------------|
| `local` | El OCR local leyó la placa con confianza sobre el umbral. Camino normal. |
| `local_confirmado` | Confianza baja, pero el VLM coincidió con el OCR local. Dos sistemas independientes de acuerdo. |
| `groq` | Confianza baja y el VLM leyó algo distinto; se usó la lectura del VLM. |
| `local_baja_confianza` | Confianza baja y el fallback no pudo ayudar (apagado, sin red, timeout). **Se devuelve la lectura local igual**, marcada. |

Con `local_baja_confianza` decide quien consume la API: mostrarla con una
advertencia, pedir otra foto o mandarla a revisión manual.

---

## Configuración

Todo por variables de entorno: en local con un `.env` (copia `.env.example`),
en Render desde **Environment**. Nada requiere tocar código.

Los dos parámetros que de verdad importan:

**`UMBRAL_CONFIANZA`** (default `0.85`) — se compara contra el carácter **más
débil** de la placa, no contra el promedio. Una placa con seis caracteres al
0.99 y uno al 0.40 está mal leída aunque promedie 0.91; el promedio te
mentiría y el mínimo no.

**`OCR_MODEL`** / **`DETECTOR_MODEL`** — precisión contra velocidad:

| | rápido | balanceado (default) | preciso |
|---|---|---|---|
| detector | `yolo-v9-t-256-...` | `yolo-v9-t-512-...` | `yolo-v9-s-608-...` |
| OCR | `cct-xs-v2-global-model` | `cct-s-v2-global-model` | `global-plates-mobile-vit-v2-model` |

Otras: `MAX_MB_IMAGEN` (default 10), `MARGEN_RECORTE` (12 px de contexto
alrededor de la placa en el recorte), `API_KEY` y `CORS_ORIGENES`. Todas están
documentadas en `.env.example`.

### Fallback opcional a Groq

Apagado por default. Si lo activas:

```ini
GROQ_HABILITADO=true
GROQ_API_KEY=gsk_...
```

Se consulta **solo** cuando la confianza local baja del umbral, y se le manda
**solo el recorte de la placa**, no la foto completa. Tres garantías del
módulo:

1. **Nunca propaga una excepción.** Timeout, 401, 429, sin red, JSON raro: se
   devuelve la lectura local y `nota_fallback` explica qué pasó.
2. **Timeout corto y explícito** (`GROQ_TIMEOUT`, default 8 s).
3. **Si el modelo ya no existe** (404: los modelos de visión de Groq están en
   *preview* y pueden desaparecer con poco aviso), reintenta una vez con
   `GROQ_MODEL_RESPALDO` y luego se rinde.

Cada consulta a Groq suma hasta `GROQ_TIMEOUT` segundos a la respuesta y gasta
cuota de tu cuenta: con Groq activo, define `API_KEY` sí o sí.

---

## Sobre las placas de Honduras

Los modelos `*-global-*` están entrenados mayormente con placas de Europa,
EE.UU. y Asia. Con formato hondureño (`HAB 1234`, `AAA 1234`) espera errores
sistemáticos: confundir `0`/`O`, `1`/`I`, o recortar el prefijo. El campo
`region` también va a equivocarse, porque Honduras no está bien representado.

La ruta para arreglarlo, **solo si `scripts/test_alpr.py` te muestra que hace
falta**:

1. Junta 300–500 fotos de placas hondureñas y etiquétalas.
2. Fine-tunea el OCR con el notebook de [`fast-plate-ocr`](https://github.com/ankandrew/fast-plate-ocr) (soporta entrenar desde cero o afinar un modelo preentrenado).
3. Carga tu modelo pasándole `ocr_model_path` y `ocr_config_path` a `ALPR(...)`
   en `app/alpr_service.py`; el resto del backend no cambia.

Es el paso más caro en tiempo. Mide primero.

---

## Estructura

```
alpr_api/
├── app/
│   ├── config.py          Variables de entorno (pydantic-settings)
│   ├── models.py          Esquemas Pydantic de la respuesta
│   ├── alpr_service.py    Detección + decisión de fallback + recorte
│   ├── groq_fallback.py   VLM sobre el recorte; nunca lanza excepciones
│   ├── icloud_service.py  Vigila la carpeta de iCloud y lee la placa de cada foto nueva
│   ├── db.py              Postgres: sesión de Apple, lecturas y dueños de las placas
│   └── main.py            Endpoints FastAPI
├── scripts/
│   ├── precargar_modelos.py   Descarga los ONNX (lo usa el Dockerfile)
│   ├── subir_sesion_icloud.py Abre la sesión de iCloud en tu PC y la guarda en Postgres
│   └── test_alpr.py           Corre el pipeline sin HTTP, para medir precisión
├── Dockerfile
├── render.yaml            Blueprint de Render
├── probar.html            Página para probar /detect desde el navegador
├── .env.example
└── requirements.txt
```

## Problemas comunes

| Síntoma | Causa y arreglo |
|---|---|
| `401` en `/detect` | Falta `X-API-Key`. En Render, el valor está en **Environment → API_KEY**. |
| La primera petición en Render tarda ~1 min | El plan gratis se durmió. No es un error. |
| `placas: []` en fotos buenas | La placa está muy pequeña o muy en ángulo. Sube a `yolo-v9-s-608-...` o baja `DETECTOR_CONF_THRESH` a 0.25. |
| Lee la placa con un carácter mal | Sube a `OCR_MODEL=global-plates-mobile-vit-v2-model`. Si el error es sistemático con placas hondureñas, toca fine-tuning. |
| El servicio se reinicia solo en Render | Probablemente se quedó sin RAM (512 MB en el plan gratis). Vuelve a los modelos default o baja `MAX_MB_IMAGEN`. |
| Error de CORS en el navegador | Tu dominio no está en `CORS_ORIGENES`. |
| `/icloud/estado` dice `falta_codigo` | Apple pidió el código (la primera vez o cada ~30 días). Ver [El código de Apple](#el-código-de-apple). |
| `409` en `POST /icloud/codigo` | La API se reinició entre los dos pasos y el código ya no sirve. Pide otro con `POST /icloud/sesion`. |
| `/icloud/estado` dice `"sesion": "rechazada"` | Apple rechazó `ICLOUD_APPLE_ID` o `ICLOUD_PASSWORD`. Tiene que ser la contraseña de la cuenta de Apple, no la del correo (y no una contraseña de app). La API no reintenta sola: corrige las variables en Render o llama `POST /icloud/sesion`. |
| Apple dice que no puede enviar códigos a ese número | Apple limita los SMS por número; espera unas horas y pide el código una sola vez. Si en tu PC hay una sesión de confianza de la cuenta, `scripts/subir_sesion_icloud.py` la sube sin código. |
| `503` "no se pudo usar Postgres" | `DATABASE_URL` está mal o la base no responde. |
| Una foto nueva tarda en salir | Render gratis estaba dormido. Ver [Cómo se mantiene al día](#cómo-se-mantiene-al-día). |
| `401` en `/propietarios` | Esas rutas piden `ADMIN_API_KEY`, no `API_KEY`. En `/docs`, **Authorize** con la de admin. |
| `503` en `/propietarios` | Falta `ADMIN_API_KEY` o `DATABASE_URL` en Render. |
| La placa está registrada pero no sale el dueño | El OCR la leyó con un carácter distinto: compara el `texto` o la `placa` de la lectura con la registrada. La comparación es exacta. |

## Créditos

- [fast-alpr](https://github.com/ankandrew/fast-alpr) (MIT) — orquestación
- [open-image-models](https://github.com/ankandrew/open-image-models) — detector YOLOv9
- [fast-plate-ocr](https://github.com/ankandrew/fast-plate-ocr) — OCR de placas
