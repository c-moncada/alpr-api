"""Postgres: la sesión de iCloud, el resultado de cada foto y los dueños de las placas.

Con Google Drive el resultado se guardaba en la propia foto (appProperties)
y no hacía falta base de datos. iCloud no tiene nada parecido y Render gratis
no tiene disco, así que aquí va todo lo que tiene que sobrevivir a un
reinicio: la sesión de Apple, el syncToken de CloudKit y las lecturas. Los
dueños de las placas (tabla propietarios) también van aquí.

Una conexión por operación: hay poco tráfico, y así no hay que vigilar
conexiones que el servidor corta cuando se suspende (Neon lo hace a los 5 min).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from app.config import settings

ESQUEMA = """
create table if not exists icloud_kv (
    clave text primary key,
    valor text not null
);
create table if not exists icloud_fotos (
    id         text primary key,  -- UUID del record documentContent/<id> en CloudKit
    nombre     text not null,
    subida     timestamptz not null,
    tamano     bigint,
    checksum   text,              -- fileChecksum: cambia si reemplazan la foto
    estado     text not null default 'pendiente',
    placa      text,
    fuente     text,
    conf_det   double precision,
    conf_ocr   double precision,
    bbox       integer[],         -- x1, y1, x2, y2
    n_placas   integer,
    nota       text,
    procesada  timestamptz
);
create index if not exists icloud_fotos_subida on icloud_fotos (subida desc);
create index if not exists icloud_fotos_placa on icloud_fotos (placa);
create table if not exists propietarios (
    placa     text primary key,  -- normalizada: solo A-Z y 0-9, como la lee el OCR
    nombre    text not null,
    telefono  text,
    correo    text,
    vehiculo  text,              -- ej. "Chevrolet Cavalier rojo"
    notas     text,
    creado    timestamptz not null default now()
);
"""

# Lo que se escribe al guardar un resultado. Van todas: las que no vienen
# quedan en null, así no sobreviven datos de otra versión de la misma foto.
RESULTADO = ("estado", "placa", "fuente", "conf_det", "conf_ocr", "bbox", "n_placas", "nota")


@contextmanager
def conectar() -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as con:
        yield con


def crear_tablas() -> None:
    with conectar() as con:
        con.execute(ESQUEMA)


# ---------------------------------------------------------------- clave/valor
def leer(clave: str) -> str | None:
    with conectar() as con:
        fila = con.execute("select valor from icloud_kv where clave = %s", (clave,)).fetchone()
    return fila["valor"] if fila else None


def guardar(clave: str, valor: str | None) -> None:
    """None borra la clave."""
    with conectar() as con:
        _guardar(con, clave, valor)


def _guardar(con: psycopg.Connection, clave: str, valor: str | None) -> None:
    if valor is None:
        con.execute("delete from icloud_kv where clave = %s", (clave,))
    else:
        con.execute(
            "insert into icloud_kv (clave, valor) values (%s, %s)"
            " on conflict (clave) do update set valor = excluded.valor",
            (clave, valor),
        )


# --------------------------------------------------------------------- fotos
def aplicar_cambios(fotos: list[dict], borradas: list[str], sync_token: str, completa: bool) -> None:
    """Registra lo que devolvió CloudKit y avanza el syncToken, todo o nada.

    Una foto nueva queda pendiente. Si ya estaba y cambió su checksum (la
    reemplazaron), vuelve a pendiente y pierde el resultado anterior. Con
    `completa` (la carpeta entera, sin syncToken) se borra lo que no vino.
    """
    with conectar() as con, con.transaction():
        if completa:
            con.execute("delete from icloud_fotos where not (id = any(%s))", ([f["id"] for f in fotos],))
        for f in fotos:
            con.execute(
                "insert into icloud_fotos (id, nombre, subida, tamano, checksum)"
                " values (%(id)s, %(nombre)s, %(subida)s, %(tamano)s, %(checksum)s)"
                " on conflict (id) do update set nombre = excluded.nombre, tamano = excluded.tamano",
                f,
            )
            con.execute(
                "update icloud_fotos set checksum = %(checksum)s, estado = 'pendiente', placa = null,"
                " fuente = null, conf_det = null, conf_ocr = null, bbox = null, n_placas = null,"
                " nota = null, procesada = null"
                " where id = %(id)s and checksum is distinct from %(checksum)s",
                f,
            )
        if borradas:
            con.execute("delete from icloud_fotos where id = any(%s)", (borradas,))
        _guardar(con, "sync_token", sync_token)


def pendientes() -> list[dict]:
    with conectar() as con:
        return con.execute(
            "select id, nombre, checksum from icloud_fotos where estado = 'pendiente' order by subida"
        ).fetchall()


def guardar_resultado(id_: str, resultado: dict, checksum: str | None) -> None:
    """`checksum` es el de la versión que se leyó.

    Si reemplazan la foto después, la siguiente revisión trae otro checksum
    y la vuelve a dejar pendiente.
    """
    valores = {k: resultado.get(k) for k in RESULTADO}
    with conectar() as con:
        con.execute(
            "update icloud_fotos set estado = %(estado)s, placa = %(placa)s, fuente = %(fuente)s,"
            " conf_det = %(conf_det)s, conf_ocr = %(conf_ocr)s, bbox = %(bbox)s,"
            " n_placas = %(n_placas)s, nota = %(nota)s, checksum = %(checksum)s, procesada = now()"
            " where id = %(id)s",
            {**valores, "checksum": checksum, "id": id_},
        )


def borrar(id_: str) -> None:
    with conectar() as con:
        con.execute("delete from icloud_fotos where id = %s", (id_,))


# El dueño de la placa como objeto, o null si no está registrada
_PROPIETARIO = (
    "case when p.placa is null then null else jsonb_build_object("
    "'nombre', p.nombre, 'telefono', p.telefono, 'correo', p.correo,"
    " 'vehiculo', p.vehiculo, 'notas', p.notas) end"
)


def listar(limite: int, placa: str | None = None) -> list[dict]:
    """Las fotos más recientes primero, con el dueño de la placa si está registrada.

    Con `placa`, solo las que tienen esa placa.
    """
    filtro = "where f.placa = %(placa)s" if placa else ""
    with conectar() as con:
        return con.execute(
            f"select f.*, {_PROPIETARIO} as propietario from icloud_fotos f"
            f" left join propietarios p on p.placa = f.placa {filtro}"
            " order by f.subida desc limit %(limite)s",
            {"placa": placa, "limite": limite},
        ).fetchall()


def obtener(id_: str) -> dict | None:
    with conectar() as con:
        return con.execute("select * from icloud_fotos where id = %s", (id_,)).fetchone()


def contar() -> dict:
    with conectar() as con:
        return con.execute(
            "select count(*) as fotos, count(*) filter (where estado = 'pendiente') as pendientes"
            " from icloud_fotos"
        ).fetchone()


# --------------------------------------------------------------- propietarios
CAMPOS_PROPIETARIO = ("nombre", "telefono", "correo", "vehiculo", "notas")


def guardar_propietario(placa: str, datos: dict) -> dict:
    """Registra el dueño de una placa (ya normalizada), o lo reemplaza si ya estaba."""
    valores = {k: datos.get(k) for k in CAMPOS_PROPIETARIO}
    with conectar() as con:
        return con.execute(
            "insert into propietarios (placa, nombre, telefono, correo, vehiculo, notas)"
            " values (%(placa)s, %(nombre)s, %(telefono)s, %(correo)s, %(vehiculo)s, %(notas)s)"
            " on conflict (placa) do update set nombre = excluded.nombre, telefono = excluded.telefono,"
            " correo = excluded.correo, vehiculo = excluded.vehiculo, notas = excluded.notas"
            " returning *",
            {**valores, "placa": placa},
        ).fetchone()


def listar_propietarios() -> list[dict]:
    with conectar() as con:
        return con.execute("select * from propietarios order by placa").fetchall()


def borrar_propietario(placa: str) -> bool:
    with conectar() as con:
        return con.execute("delete from propietarios where placa = %s", (placa,)).rowcount > 0


def propietarios_de(placas: list[str]) -> dict[str, dict]:
    """Los dueños registrados de estas placas, por placa."""
    with conectar() as con:
        filas = con.execute("select * from propietarios where placa = any(%s)", (placas,)).fetchall()
    return {f["placa"]: f for f in filas}
