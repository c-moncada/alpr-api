# API de Reconocimiento de Placas Vehiculares (ALPR)

Backend FastAPI: le mandas la foto de un vehículo y te devuelve **el texto de
la placa y la foto recortada de la placa**. No guarda nada (ni imágenes ni
historial), así que corre en cualquier contenedor sin disco persistente.

La detección y el OCR corren con modelos ONNX locales: sin API key, sin
tokens, sin cuota. El único servicio externo es un fallback opcional a Groq,
apagado por default.

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
| `GET` | `/drive/lecturas` | Fotos recientes de la carpeta de Drive con su placa (`?placa=HAB1234` para buscar) |
| `GET` | `/drive/lecturas/{drive_id}/recorte` | Recorte JPEG de la placa de una foto de Drive |
| `POST` | `/drive/escanear` | Revisa la carpeta ya, sin esperar el aviso de Google |
| `GET` | `/drive/estado` | Canal de avisos, último escaneo y último error |
| `POST` | `/drive/webhook` | Lo llama Google, no tú. Valida un token propio en vez de la API key. |

Los `/drive/*` responden `503` si la vigilancia de Drive no está configurada
(ver [Vigilar una carpeta de Google Drive](#vigilar-una-carpeta-de-google-drive)).

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
3. Te va a pedir `GROQ_API_KEY`: déjala vacía si no usas Groq. `API_KEY` la
   genera Render sola; la copias desde **Environment** en el panel del servicio.
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
- **`CORS_ORIGENES`**: qué dominios pueden llamar a la API desde un navegador,
  separados por coma (ej. `https://miapp.com`). El default `*` acepta
  cualquiera; cuando tengas el frontend publicado, pon su dominio.

---

## Vigilar una carpeta de Google Drive

Opcional y apagado por default. Subes fotos a una carpeta de Drive, la API
se entera sola y lee la placa de cada una:

```
foto nueva en Drive ──aviso──▶ POST /drive/webhook ──▶ escanea la carpeta
                                                              │
      React Native ◀── GET /drive/lecturas ◀── placa guardada en la foto
```

**El aviso de Google no dice qué archivo cambió**, solo que algo cambió. Cada
aviso dispara un escaneo que procesa las fotos de la carpeta que todavía no
tienen resultado.

**El resultado se guarda en la propia foto**, como `appProperties`: metadatos
privados de esta app que el usuario no ve en Drive. Por eso la API sigue sin
base de datos ni disco: si Render reinicia o duerme el contenedor, al
arrancar escanea la carpeta y retoma donde se quedó.

Por cada foto se guarda la placa más clara (`placas[0]`), su bbox y cuántas
placas había. El recorte no se guarda: `/drive/lecturas/{id}/recorte` lo
vuelve a cortar de la foto original con el bbox, sin correr otra vez el modelo.

### Configuración

1. En [Google Cloud Console](https://console.cloud.google.com): crea un
   proyecto, habilita **Google Drive API** y crea una **service account**. En
   su pestaña *Keys* → *Add key* → *JSON*, descarga el archivo.
2. En Drive, **comparte la carpeta con el email de la service account**
   (`...@....iam.gserviceaccount.com`) **como Editor**. Lector no alcanza: la
   API escribe el resultado en cada foto.
3. Variables de entorno:

   ```ini
   DRIVE_HABILITADO=true
   DRIVE_CARPETA_ID=1AbC...        # lo que va después de /folders/ en la URL
   # local: el archivo JSON junto al proyecto (ya está en .gitignore)
   GOOGLE_SERVICE_ACCOUNT_FILE=service-account.json
   # Render: el JSON completo en una sola variable
   GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}
   ```

4. **El webhook necesita una URL HTTPS pública.** En Render se toma sola de
   `RENDER_EXTERNAL_URL`. En local, levanta un túnel (`ngrok http 8000`) y
   pon `URL_PUBLICA=https://xxxx.ngrok-free.app`. Sin URL pública la API
   funciona igual, pero solo revisa la carpeta cada `DRIVE_INTERVALO_REVISION`
   segundos (default 600), o cuando llamas a `POST /drive/escanear`.

Al arrancar, el log dice `Vigilando la carpeta de Drive ...` y, si hay
webhook, `Canal de Drive activo hasta ...`.

### Cómo se mantiene vivo

- Google cierra el canal de avisos a los **7 días** como máximo. La API lo
  renueva sola cuando le queda menos de un día, y al arrancar crea uno nuevo
  y detiene el anterior (su ID queda guardado en la carpeta).
- Cada `DRIVE_INTERVALO_REVISION` segundos escanea la carpeta aunque no
  llegue ningún aviso: si Google pierde uno, la foto se procesa igual.
- **Plan gratis de Render:** el aviso de Google despierta al contenedor, y
  el escaneo de arranque procesa la foto. Tarda lo que tarde en despertar
  (~1 min). Si el servicio pasa **más de 7 días dormido**, el canal caduca y
  ya nada lo despierta: la siguiente petición cualquiera lo reactiva y el
  escaneo de arranque recoge todo lo pendiente.

### El campo `estado` de cada lectura

| Valor | Significado |
|---|---|
| `pendiente` | Todavía no se procesa |
| `ok` | Se encontró al menos una placa (`placa` puede ser `null` si el OCR no leyó nada) |
| `sin_placa` | El detector no encontró ninguna placa |
| `no_decodificable` | OpenCV no puede abrir el archivo. Típico: fotos **HEIC** de iPhone. Configura la cámara en "Más compatible" (JPG). |
| `muy_grande` | Pesa más de `MAX_MB_IMAGEN` |
| `error` | Falló el pipeline; el detalle va en `nota` |

Una foto se procesa una sola vez. Para volver a leerla, súbela de nuevo.

### Desde React Native

```js
const API = "https://TU-SERVICIO.onrender.com";
const headers = { "X-API-Key": API_KEY };

const { lecturas } = await (await fetch(`${API}/drive/lecturas?limite=20`, { headers })).json();

// El recorte de la placa, con el header de la API key
<Image
  source={{ uri: `${API}/drive/lecturas/${lecturas[0].drive_id}/recorte`, headers }}
  style={{ width: 200, height: 60 }}
/>
```

La API key queda dentro de la app, igual que en una página web: frena el
abuso casual pero no es un secreto.

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
│   ├── drive_service.py   Vigila la carpeta de Drive y guarda la placa en cada foto
│   └── main.py            Endpoints FastAPI
├── scripts/
│   ├── precargar_modelos.py   Descarga los ONNX (lo usa el Dockerfile)
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

## Créditos

- [fast-alpr](https://github.com/ankandrew/fast-alpr) (MIT) — orquestación
- [open-image-models](https://github.com/ankandrew/open-image-models) — detector YOLOv9
- [fast-plate-ocr](https://github.com/ankandrew/fast-plate-ocr) — OCR de placas
