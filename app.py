import calendar as calendar_mod
import hashlib
import io
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from supabase import create_client
import speech_recognition as sr

# ==========================================
# CONFIGURACIÓN DE PÁGINA
# ==========================================
st.set_page_config(
    page_title="Gestor de Tareas Sincronizado",
    page_icon="📱",
    layout="wide",
    initial_sidebar_state="expanded",
)

MINUTOS_ALERTA = 15
BUCKET_ADJUNTOS = "adjuntos"

# ==========================================
# ZONA HORARIA (Buenos Aires) — clave para que las alertas de
# "faltan 15 minutos" se disparen en el momento correcto sin
# importar en qué huso horario corra el servidor (Streamlit Cloud
# suele correr en UTC).
# ==========================================
ZONA_HORARIA = ZoneInfo("America/Buenos_Aires")


def ahora_local():
    """Hora actual en Buenos Aires, como datetime 'naive' (sin tzinfo).

    Se le quita el tzinfo a propósito: las fechas/horas que carga el
    usuario en el formulario también son 'naive' (representan la hora
    de pared de Buenos Aires), así que comparamos siempre naive-contra-
    naive, ambas en la misma zona horaria real."""
    return datetime.now(ZONA_HORARIA).replace(tzinfo=None)


# ==========================================
# PWA: hacer la app instalable (icono en pantalla de inicio)
# ==========================================
st.components.v1.html(
    """
    <script>
    (function () {
        const doc = window.parent.document;

        if (!doc.querySelector('link[rel="manifest"][data-app-pwa]')) {
            const link = doc.createElement('link');
            link.rel = 'manifest';
            link.href = '/app/static/manifest.json';
            link.setAttribute('data-app-pwa', 'true');
            doc.head.appendChild(link);
        }

        if (!doc.querySelector('meta[name="theme-color"][data-app-pwa]')) {
            const meta = doc.createElement('meta');
            meta.name = 'theme-color';
            meta.content = '#4f6df5';
            meta.setAttribute('data-app-pwa', 'true');
            doc.head.appendChild(meta);
        }

        if (!doc.querySelector('link[rel="apple-touch-icon"][data-app-pwa]')) {
            const appleIcon = doc.createElement('link');
            appleIcon.rel = 'apple-touch-icon';
            appleIcon.href = '/app/static/icon-192.png';
            appleIcon.setAttribute('data-app-pwa', 'true');
            doc.head.appendChild(appleIcon);
        }

        if (window.parent.navigator && window.parent.navigator.serviceWorker) {
            window.parent.navigator.serviceWorker
                .register('/app/static/service-worker.js')
                .catch(function (err) {
                    console.warn('No se pudo registrar el service worker:', err);
                });
        }
    })();
    </script>
    """,
    height=0,
)

SUPABASE_URL = st.secrets.get(
    "SUPABASE_URL", os.environ.get("SUPABASE_URL", "")
)
SUPABASE_KEY = st.secrets.get(
    "SUPABASE_KEY", os.environ.get("SUPABASE_KEY", "")
)

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("⚠️ Faltan las credenciales de Supabase en .streamlit/secrets.toml")
    st.stop()


@st.cache_resource
def init_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


supabase = init_supabase()

# Refresca el script solo (sin recargar la pagina) cada 20 segundos, para
# que las alertas de "faltan 15 minutos" se disparen aunque no estes
# tocando nada en pantalla. OJO: esto solo funciona mientras la pestaña
# esta ABIERTA Y EN PRIMER PLANO — si el celular se bloquea o cambiás de
# app, el navegador pausa estos temporizadores (limitación del navegador,
# no de la app). Ver mas detalle en la respuesta del chat.
st_autorefresh(interval=20_000, limit=None, key="autorefresh_alertas")

# ==========================================
# TRANSCRIPCIÓN DE VOZ A TEXTO
# ==========================================
IDIOMA_RECONOCIMIENTO = "es-AR"


def transcribir_audio(audio_bytes: bytes, idioma: str = IDIOMA_RECONOCIMIENTO):
    reconocedor = sr.Recognizer()
    try:
        with sr.AudioFile(io.BytesIO(audio_bytes)) as fuente:
            datos_audio = reconocedor.record(fuente)
        texto = reconocedor.recognize_google(datos_audio, language=idioma)
        texto = texto.strip()
        if not texto:
            return None, "No se detectó ninguna palabra en el audio."
        return texto, None
    except sr.UnknownValueError:
        return None, "No se entendió el audio. Probá hablar más claro y cerca del micrófono."
    except sr.RequestError as e:
        return None, f"No se pudo conectar con el servicio de reconocimiento de voz ({e})."
    except Exception as e:
        return None, f"Error inesperado al procesar el audio ({e})."


# ==========================================
# FUNCIONES DE BASE DE DATOS — TAREAS
# ==========================================
def _normalizar_columnas_tareas(df):
    """Por si la migración de proyecto_id/adjuntos todavía no se corrió:
    evita que la app se rompa, simplemente esas funciones no van a tener
    efecto hasta que corras el SQL correspondiente."""
    for col in ("proyecto_id", "adjunto_path", "adjunto_nombre"):
        if col not in df.columns:
            df[col] = None
    return df


def cargar_tareas():
    try:
        res = (
            supabase.from_("tareas")
            .select("*")
            .order("fecha", desc=False)
            .order("hora", desc=False)
            .execute()
        )
        df = pd.DataFrame(res.data)
        return _normalizar_columnas_tareas(df)
    except Exception as e:
        st.error(f"Error al cargar tareas: {e}")
        return pd.DataFrame()


def agregar_tarea(titulo, categoria, responsable, fecha, hora, notas, proyecto_id=None):
    data = {
        "titulo": titulo,
        "categoria": categoria,
        "responsable": responsable,
        "fecha": str(fecha),
        "hora": str(hora),
        "estado": "Pendiente",
        "avance": 0,
        "notas": notas,
        "alertado": False,
        "proyecto_id": proyecto_id,
    }
    supabase.from_("tareas").insert(data).execute()


def actualizar_tarea_completa(id_tarea, avance, notas, fecha, hora, proyecto_id=None):
    estado = "Superado" if avance >= 100 else "Pendiente"
    data = {
        "estado": estado,
        "avance": avance,
        "notas": notas,
        "fecha": str(fecha),
        "hora": str(hora),
        "alertado": False,
        "proyecto_id": proyecto_id,
    }
    supabase.from_("tareas").update(data).eq("id", id_tarea).execute()


def marcar_alertado(id_tarea):
    supabase.from_("tareas").update({"alertado": True}).eq("id", id_tarea).execute()


def eliminar_tarea(id_tarea):
    supabase.from_("tareas").delete().eq("id", id_tarea).execute()


# ==========================================
# FUNCIONES DE BASE DE DATOS — PROYECTOS
# ==========================================
def cargar_proyectos():
    try:
        res = supabase.from_("proyectos").select("*").order("nombre").execute()
        return pd.DataFrame(res.data)
    except Exception as e:
        st.error(f"Error al cargar proyectos (¿ya corriste la migración SQL?): {e}")
        return pd.DataFrame()


def crear_proyecto(nombre, categoria, responsable, notas):
    supabase.from_("proyectos").insert(
        {"nombre": nombre, "categoria": categoria, "responsable": responsable, "notas": notas}
    ).execute()


def eliminar_proyecto(proyecto_id):
    # Las subtareas quedan sueltas (proyecto_id vuelve a NULL) gracias al
    # "on delete set null" de la migración SQL — no se borran solas.
    supabase.from_("proyectos").delete().eq("id", proyecto_id).execute()


# ==========================================
# ADJUNTOS (Supabase Storage)
# ==========================================
def subir_adjunto(tarea_id, archivo):
    extension = os.path.splitext(archivo.name)[1]
    path = f"{tarea_id}/{int(time.time())}{extension}"
    contenido = archivo.getvalue()
    supabase.storage.from_(BUCKET_ADJUNTOS).upload(
        path,
        contenido,
        file_options={"content-type": archivo.type or "application/octet-stream"},
    )
    supabase.from_("tareas").update(
        {"adjunto_path": path, "adjunto_nombre": archivo.name}
    ).eq("id", tarea_id).execute()


def eliminar_adjunto(tarea_id, path):
    if path:
        try:
            supabase.storage.from_(BUCKET_ADJUNTOS).remove([path])
        except Exception:
            pass
    supabase.from_("tareas").update(
        {"adjunto_path": None, "adjunto_nombre": None}
    ).eq("id", tarea_id).execute()


def url_adjunto(path):
    return supabase.storage.from_(BUCKET_ADJUNTOS).get_public_url(path)


# ==========================================
# CLASIFICACIÓN (Superada / Agendada / Pendiente)
# ==========================================
def _parsear_fecha_hora(fecha, hora):
    hora_str = str(hora)
    if len(hora_str) == 5:
        hora_str += ":00"
    return datetime.strptime(f"{fecha} {hora_str}", "%Y-%m-%d %H:%M:%S")


def clasificar_tarea(row, ahora):
    if row["estado"] == "Superado":
        return "Superada"
    try:
        fh = _parsear_fecha_hora(row["fecha"], row["hora"])
    except Exception:
        return "Pendiente"
    return "Agendada" if fh >= ahora else "Pendiente"


# ==========================================
# SISTEMA DE ALERTAS (una sola vez por tarea, 15 min antes)
# ==========================================
def obtener_tareas_para_alertar(df, ahora):
    if df.empty:
        return []
    resultado = []
    df_activas = df[df["estado"] != "Superado"]
    for _, row in df_activas.iterrows():
        if bool(row.get("alertado", False)):
            continue
        try:
            fh = _parsear_fecha_hora(row["fecha"], row["hora"])
        except Exception:
            continue
        diferencia_minutos = (fh - ahora).total_seconds() / 60
        if 0 <= diferencia_minutos <= MINUTOS_ALERTA:
            resultado.append((row, int(diferencia_minutos)))
    return resultado


def verificar_alertas(df, ahora):
    for row, minutos_restantes in obtener_tareas_para_alertar(df, ahora):
        marcar_alertado(row["id"])

        st.error(
            f"🚨 **¡Tarea próxima! (en {minutos_restantes} min)**\n\n"
            f"📌 **{row['titulo']}**  |  📂 {row['categoria']}  |  ⏰ {str(row['hora'])[:5]}"
        )
        st.toast(f"⏰ En {minutos_restantes} min: {row['titulo']}", icon="🚨")

        titulo_js = json.dumps(str(row["titulo"]))
        st.components.v1.html(
            f"""
            <script>
            (function () {{
                const p = window.parent;
                try {{
                    if (p.Notification && p.Notification.permission === 'granted') {{
                        new p.Notification('⏰ Tarea en {minutos_restantes} min', {{
                            body: {titulo_js},
                            icon: '/app/static/icon-192.png'
                        }});
                    }}
                }} catch (e) {{}}
                try {{
                    if (p.__appAudioCtx) {{
                        const ctx = p.__appAudioCtx;
                        const osc = ctx.createOscillator();
                        const gain = ctx.createGain();
                        osc.type = 'sine';
                        osc.frequency.value = 880;
                        gain.gain.setValueAtTime(0.2, ctx.currentTime);
                        osc.connect(gain).connect(ctx.destination);
                        osc.start();
                        osc.stop(ctx.currentTime + 0.35);
                    }}
                }} catch (e) {{}}
            }})();
            </script>
            """,
            height=0,
        )


def boton_activar_alertas():
    """Boton real (no de Streamlit) que:
    - Refleja si las notificaciones ya estaban concedidas de antes (para
      no mostrar siempre 'sin activar' aunque ya lo hayas hecho).
    - Reproduce un beep de PRUEBA al tocarlo, para que confirmes al toque
      si el sonido funciona en ese dispositivo."""
    st.components.v1.html(
        """
        <div style="font-family: 'Segoe UI', sans-serif;">
          <button id="btn-activar-alertas" style="background:#4f6df5;color:white;border:none;
                  padding:10px 16px;border-radius:8px;font-size:14px;cursor:pointer;">
            🔔 Activar alertas sonoras y notificaciones en este dispositivo
          </button>
          <span id="estado-alertas" style="margin-left:10px;font-size:13px;color:#444;"></span>
        </div>
        <script>
        const p = window.parent;
        const boton = document.getElementById('btn-activar-alertas');
        const estado = document.getElementById('estado-alertas');

        function actualizarEstadoVisual() {
            const permisoNotif = (p.Notification && p.Notification.permission) || 'default';
            const audioListo = !!p.__appAudioCtx;
            if (permisoNotif === 'granted' && audioListo) {
                boton.innerText = '🔔 Alertas activadas en este dispositivo (tocá para probar el sonido)';
                estado.innerText = '✅ Notificaciones listas';
            } else if (permisoNotif === 'denied') {
                estado.innerText = '⚠️ Notificaciones bloqueadas en este navegador. El cartel rojo en pantalla igual va a avisarte.';
            }
        }

        try {
            if (!p.__appAudioCtx) {
                p.__appAudioCtx = new (p.AudioContext || p.webkitAudioContext)();
            }
        } catch (e) {}

        actualizarEstadoVisual();

        boton.addEventListener('click', function () {
            try {
                if (!p.__appAudioCtx) {
                    p.__appAudioCtx = new (p.AudioContext || p.webkitAudioContext)();
                }
                p.__appAudioCtx.resume();
                const ctx = p.__appAudioCtx;
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'sine';
                osc.frequency.value = 880;
                gain.gain.setValueAtTime(0.25, ctx.currentTime);
                osc.connect(gain).connect(ctx.destination);
                osc.start();
                osc.stop(ctx.currentTime + 0.3);
            } catch (e) {}

            if (p.Notification && p.Notification.requestPermission) {
                p.Notification.requestPermission().then(function () {
                    actualizarEstadoVisual();
                });
            } else {
                actualizarEstadoVisual();
            }
        });
        </script>
        """,
        height=55,
    )


# ==========================================
# ESTADO DEL FORMULARIO (sidebar)
# ==========================================
def _hora_por_defecto():
    return (ahora_local() + timedelta(minutes=30)).time()


def _valores_por_defecto_formulario():
    return {
        "titulo_input": "",
        "notas_input": "",
        "responsable_input": "Yo",
        "categoria_input": "ENRESP",
        "fecha_input": ahora_local().date(),
        "hora_input": _hora_por_defecto(),
        "proyecto_input": "Ninguno",
        "ultimo_audio_hash": "",
    }


if st.session_state.get("_reset_formulario", False):
    for clave, valor in _valores_por_defecto_formulario().items():
        st.session_state[clave] = valor
    st.session_state["_reset_formulario"] = False

for clave, valor in _valores_por_defecto_formulario().items():
    if clave not in st.session_state:
        st.session_state[clave] = valor

# ==========================================
# BARRA LATERAL
# ==========================================
proyectos_df_sidebar = cargar_proyectos()
opciones_proyecto = ["Ninguno"] + (
    list(proyectos_df_sidebar["nombre"]) if not proyectos_df_sidebar.empty else []
)

st.sidebar.title("📌 Agendar Tarea")

modo_ingreso = st.sidebar.radio(
    "Método de entrada:", ["✍️ Formulario Manual", "🎙️ Grabar Audio de Voz"]
)

if modo_ingreso == "🎙️ Grabar Audio de Voz":
    st.sidebar.markdown("**Presiona el micrófono y habla:**")
    audio_file = st.sidebar.audio_input("Grabar nota")

    if audio_file is not None:
        audio_bytes = audio_file.getvalue()
        audio_hash = hashlib.md5(audio_bytes).hexdigest()

        if st.session_state.ultimo_audio_hash != audio_hash:
            with st.sidebar:
                with st.spinner("🎧 Transcribiendo audio..."):
                    texto, error = transcribir_audio(audio_bytes)

            st.session_state.ultimo_audio_hash = audio_hash

            if texto:
                st.session_state.titulo_input = texto[:80]
                st.session_state.notas_input = texto
                st.sidebar.success(f"✅ Transcripto: “{texto[:60]}{'...' if len(texto) > 60 else ''}”")
            else:
                st.sidebar.error(f"⚠️ No se pudo transcribir el audio: {error}")

with st.sidebar.form("form_tarea", clear_on_submit=False):
    titulo = st.text_input("Título de la Tarea", key="titulo_input")
    categoria = st.selectbox("Categoría", ["ENRESP", "EXTERNO"], key="categoria_input")
    responsable = st.text_input("Responsable", key="responsable_input")
    fecha = st.date_input("Fecha", key="fecha_input")
    hora = st.time_input("Hora de inicio", key="hora_input")
    proyecto_sel = st.selectbox("Proyecto (opcional)", opciones_proyecto, key="proyecto_input")
    notas = st.text_area("Notas / Minuta inicial", key="notas_input")

    submitted = st.form_submit_button("➕ Agendar Tarea", use_container_width=True)

    if submitted:
        if titulo.strip() != "":
            proyecto_id_nuevo = None
            if proyecto_sel != "Ninguno" and not proyectos_df_sidebar.empty:
                coincidencia = proyectos_df_sidebar[proyectos_df_sidebar["nombre"] == proyecto_sel]
                if not coincidencia.empty:
                    proyecto_id_nuevo = int(coincidencia.iloc[0]["id"])

            agregar_tarea(titulo, categoria, responsable, fecha, hora, notas, proyecto_id_nuevo)

            st.toast("🎉 ¡Tarea agregada con éxito!", icon="✅")
            st.sidebar.success("✅ Tarea registrada en la base de datos.")

            st.session_state["_reset_formulario"] = True
            time.sleep(1)
            st.rerun()
        else:
            st.sidebar.error("⚠️ Debes colocar un título a la tarea.")

st.sidebar.markdown("---")
with st.sidebar.expander("🗂️ Crear nuevo proyecto"):
    with st.form("form_proyecto", clear_on_submit=True):
        nombre_proy = st.text_input("Nombre del proyecto")
        categoria_proy = st.selectbox("Categoría", ["ENRESP", "EXTERNO"], key="categoria_proyecto_input")
        responsable_proy = st.text_input("Responsable", value="Yo", key="responsable_proyecto_input")
        notas_proy = st.text_area("Notas", key="notas_proyecto_input")
        crear = st.form_submit_button("➕ Crear proyecto")
        if crear:
            if nombre_proy.strip():
                crear_proyecto(nombre_proy, categoria_proy, responsable_proy, notas_proy)
                st.sidebar.success("✅ Proyecto creado.")
                st.rerun()
            else:
                st.sidebar.error("⚠️ Ponele un nombre al proyecto.")

# ==========================================
# PANEL DE EDICIÓN COMPARTIDO
# ==========================================
def panel_edicion(tarea_id, df, proyectos_df, ahora):
    fila = df[df["id"] == tarea_id]
    if fila.empty:
        return
    row = fila.iloc[0]

    clas = clasificar_tarea(row, ahora)
    badge = {"Superada": "🟢 Superada", "Agendada": "🔵 Agendada", "Pendiente": "🔴 Pendiente"}[clas]
    st.markdown(f"**{row['titulo']}** — {badge}")

    try:
        fecha_actual = pd.to_datetime(row["fecha"]).date()
    except Exception:
        fecha_actual = ahora.date()
    try:
        hora_actual = datetime.strptime(str(row["hora"])[:5], "%H:%M").time()
    except Exception:
        hora_actual = ahora.time()

    col_a, col_b = st.columns(2)
    with col_a:
        nueva_fecha = st.date_input("Fecha", value=fecha_actual, key=f"ed_fecha_{tarea_id}")
    with col_b:
        nueva_hora = st.time_input("Hora", value=hora_actual, key=f"ed_hora_{tarea_id}")

    pct = st.slider("% Cumplimiento", 0, 100, int(row["avance"]), key=f"ed_pct_{tarea_id}")
    nuevas_notas = st.text_area(
        "Notas / Acuerdos", value=row["notas"] if row["notas"] else "", key=f"ed_notas_{tarea_id}"
    )

    opciones_ed = ["Ninguno"] + (list(proyectos_df["nombre"]) if not proyectos_df.empty else [])
    nombre_proy_actual = "Ninguno"
    if pd.notna(row.get("proyecto_id")) and not proyectos_df.empty:
        coincidencia = proyectos_df[proyectos_df["id"] == row["proyecto_id"]]
        if not coincidencia.empty:
            nombre_proy_actual = coincidencia.iloc[0]["nombre"]
    idx_actual = opciones_ed.index(nombre_proy_actual) if nombre_proy_actual in opciones_ed else 0
    proyecto_elegido = st.selectbox("Proyecto", opciones_ed, index=idx_actual, key=f"ed_proy_{tarea_id}")

    if pct >= 100:
        st.success("Al guardar queda como **Superada**.")
    else:
        fh_nueva = datetime.combine(nueva_fecha, nueva_hora)
        if fh_nueva >= ahora:
            st.info("Al guardar queda como **Agendada** (fecha/hora futura).")
        else:
            st.warning("Al guardar queda como **Pendiente**. Elegí una fecha/hora futura para agendarla.")

    st.markdown("📎 **Adjunto**")
    if row.get("adjunto_nombre"):
        try:
            url = url_adjunto(row["adjunto_path"])
            st.markdown(f"[{row['adjunto_nombre']}]({url})")
        except Exception:
            st.caption(f"{row['adjunto_nombre']} (no se pudo generar el link)")
        if st.button("🗑️ Quitar adjunto", key=f"ed_quitar_adj_{tarea_id}"):
            eliminar_adjunto(tarea_id, row["adjunto_path"])
            st.rerun()
    else:
        st.caption("Sin archivo adjunto todavía.")

    nuevo_archivo = st.file_uploader("Subir / reemplazar adjunto", key=f"ed_file_{tarea_id}")
    if nuevo_archivo is not None:
        if st.button("⬆️ Guardar adjunto", key=f"ed_subir_{tarea_id}"):
            try:
                subir_adjunto(tarea_id, nuevo_archivo)
                st.toast("📎 Adjunto guardado")
                st.rerun()
            except Exception as e:
                st.error(f"No se pudo subir el adjunto (¿ya creaste el bucket 'adjuntos' en Supabase?): {e}")

    proyecto_id_final = None
    if proyecto_elegido != "Ninguno":
        coincidencia = proyectos_df[proyectos_df["nombre"] == proyecto_elegido]
        if not coincidencia.empty:
            proyecto_id_final = int(coincidencia.iloc[0]["id"])

    col_g, col_d = st.columns(2)
    with col_g:
        if st.button("💾 Guardar cambios", key=f"ed_guardar_{tarea_id}", use_container_width=True):
            actualizar_tarea_completa(tarea_id, pct, nuevas_notas, nueva_fecha, nueva_hora, proyecto_id_final)
            for k in (
                f"ed_fecha_{tarea_id}", f"ed_hora_{tarea_id}", f"ed_pct_{tarea_id}",
                f"ed_notas_{tarea_id}", f"ed_proy_{tarea_id}",
            ):
                st.session_state.pop(k, None)
            st.toast("Guardado correctamente")
            st.rerun()
    with col_d:
        if st.button("🗑️ Eliminar tarea", key=f"ed_eliminar_{tarea_id}", use_container_width=True):
            eliminar_tarea(tarea_id)
            st.rerun()


# ==========================================
# TABLA DE UN GRUPO (con selección de fila)
# ==========================================
COLUMNAS_TABLA = ["clasificacion", "titulo", "fecha", "hora", "categoria", "responsable", "avance", "notas"]
NOMBRES_COLUMNAS = ["Clasificación", "Título", "Fecha", "Hora", "Categoría", "Responsable", "Avance %", "Notas"]


def mostrar_tabla(df_grupo, key, mostrar_columna_proyecto=False):
    if df_grupo.empty:
        st.caption("No hay tareas en esta clasificación.")
        return None

    vista = df_grupo[COLUMNAS_TABLA].copy()
    nombres = list(NOMBRES_COLUMNAS)

    if mostrar_columna_proyecto:
        # Insertamos "Proyecto" justo despues de "Categoria" (4to lugar)
        vista.insert(4, "proyecto_nombre", df_grupo["proyecto_nombre"])
        nombres = nombres[:4] + ["Proyecto"] + nombres[4:]

    vista["📎"] = df_grupo["adjunto_nombre"].apply(lambda x: "📎" if x else "")
    vista.columns = nombres + ["📎"]

    evento = st.dataframe(
        vista,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=key,
    )
    if evento and evento["selection"]["rows"]:
        idx_local = evento["selection"]["rows"][0]
        return int(df_grupo.iloc[idx_local]["id"])
    return None


# ==========================================
# CALENDARIO DEL MES ACTUAL
# ==========================================
NOMBRES_MES_ES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]
DIAS_SEMANA_ES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

# (color de fondo, color de texto) de cada "barrita" de tarea, estilo Google Calendar
COLOR_CLASIFICACION = {
    "Superada": "#22c55e",
    "Agendada": "#3b82f6",
    "Pendiente": "#ef4444",
}
MAX_TAREAS_VISIBLES_POR_DIA = 3


def _escapar_html(texto):
    return (
        str(texto)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def mostrar_calendario_mes(df, ahora):
    hoy = ahora.date()
    st.markdown(
        f"<div style='font-size:1.1rem;font-weight:700;color:#1e293b;margin-bottom:0.6rem;'>"
        f"{NOMBRES_MES_ES[hoy.month]} {hoy.year}</div>",
        unsafe_allow_html=True,
    )

    tareas_por_dia = {}
    if not df.empty:
        for _, row in df.iterrows():
            try:
                f = pd.to_datetime(row["fecha"]).date()
            except Exception:
                continue
            if f.year == hoy.year and f.month == hoy.month:
                tareas_por_dia.setdefault(f.day, []).append(row)

    semanas = calendar_mod.Calendar(firstweekday=0).monthdayscalendar(hoy.year, hoy.month)

    # Encabezado de días de la semana
    celdas_encabezado = "".join(
        f"<div style='flex:1;text-align:center;font-size:11px;font-weight:700;"
        f"color:#64748b;text-transform:uppercase;letter-spacing:0.04em;padding-bottom:6px;'>"
        f"{nombre}</div>"
        for nombre in DIAS_SEMANA_ES
    )

    filas_html = ""
    for semana in semanas:
        celdas_html = ""
        for dia in semana:
            if dia == 0:
                celdas_html += (
                    "<div style='flex:1;min-height:100px;margin:2px;'></div>"
                )
                continue

            es_hoy_dia = dia == hoy.day
            tareas_dia = sorted(
                tareas_por_dia.get(dia, []), key=lambda r: str(r["hora"])
            )

            if es_hoy_dia:
                numero_html = (
                    "<span style='background:#3b82f6;color:#ffffff;border-radius:999px;"
                    "width:22px;height:22px;display:inline-flex;align-items:center;"
                    "justify-content:center;font-weight:700;font-size:12px;'>"
                    f"{dia}</span>"
                )
            else:
                numero_html = (
                    f"<span style='color:#1e293b;font-weight:600;font-size:12px;'>{dia}</span>"
                )

            barras_html = ""
            for t in tareas_dia[:MAX_TAREAS_VISIBLES_POR_DIA]:
                clas = clasificar_tarea(t, ahora)
                color = COLOR_CLASIFICACION.get(clas, "#94a3b8")
                hora_corta = str(t["hora"])[:5]
                titulo_full = _escapar_html(t["titulo"])
                barras_html += (
                    f"<div title='{hora_corta} {titulo_full}' style='background:{color};"
                    "color:#ffffff;border-radius:4px;padding:1px 5px;margin-top:3px;"
                    "font-size:10.5px;line-height:15px;white-space:nowrap;overflow:hidden;"
                    f"text-overflow:ellipsis;'>{hora_corta} {titulo_full}</div>"
                )
            restantes = len(tareas_dia) - MAX_TAREAS_VISIBLES_POR_DIA
            if restantes > 0:
                barras_html += (
                    f"<div style='font-size:10px;color:#64748b;margin-top:2px;'>"
                    f"+{restantes} más</div>"
                )

            fondo = "#eff6ff" if es_hoy_dia else "#ffffff"
            borde = "1.5px solid #3b82f6" if es_hoy_dia else "1px solid #e2e8f0"
            celdas_html += (
                f"<div style='flex:1;min-height:100px;margin:2px;padding:6px;"
                f"background:{fondo};border:{borde};border-radius:8px;overflow:hidden;'>"
                f"{numero_html}{barras_html}</div>"
            )
        filas_html += f"<div style='display:flex;'>{celdas_html}</div>"

    html_completo = (
        "<div style='font-family:\"Segoe UI\", Roboto, sans-serif;'>"
        f"<div style='display:flex;'>{celdas_encabezado}</div>"
        f"{filas_html}"
        "</div>"
    )
    st.markdown(html_completo, unsafe_allow_html=True)

    st.markdown(
        "<div style='margin-top:0.6rem;font-size:12.5px;color:#475569;'>"
        "<span style='background:#22c55e;color:#fff;border-radius:4px;padding:1px 6px;'>Superada</span>"
        "&nbsp;&nbsp;"
        "<span style='background:#3b82f6;color:#fff;border-radius:4px;padding:1px 6px;'>Programada / Agendada</span>"
        "&nbsp;&nbsp;"
        "<span style='background:#ef4444;color:#fff;border-radius:4px;padding:1px 6px;'>Pendiente</span>"
        "</div>",
        unsafe_allow_html=True,
    )


# ==========================================
# PANEL PRINCIPAL
# ==========================================
st.title("Planificación de Proyectos")

boton_activar_alertas()
st.caption(
    "Tocá el botón de arriba una vez por dispositivo (celular y PC por separado) para "
    "recibir sonido y notificación cuando falte 15 minutos para una tarea. El cartel rojo "
    "en pantalla funciona siempre, sin necesidad de activarlo."
)

df = cargar_tareas()
proyectos_df = cargar_proyectos()
ahora = ahora_local()
hoy_str = ahora.strftime("%Y-%m-%d")

verificar_alertas(df, ahora)
st.caption(
    f"🔄 Última revisión de alertas: {ahora.strftime('%H:%M:%S')} (hora Argentina) "
    "(se repite sola cada 20 segundos mientras esta pantalla esté abierta)"
)
st.caption(f"📅 Día actual: **{ahora.strftime('%d/%m/%Y')}**")

id_click = None

if df.empty and proyectos_df.empty:
    st.info("No hay tareas ni proyectos registrados todavía. Agregá el primero desde la barra lateral.")
else:
    id_a_nombre_proyecto = {}
    if not proyectos_df.empty:
        id_a_nombre_proyecto = {int(r["id"]): r["nombre"] for _, r in proyectos_df.iterrows()}

    df_todas = df.copy()
    if not df_todas.empty:
        df_todas["clasificacion"] = df_todas.apply(lambda r: clasificar_tarea(r, ahora), axis=1)
        df_todas["proyecto_nombre"] = df_todas["proyecto_id"].apply(
            lambda x: id_a_nombre_proyecto.get(int(x), "") if pd.notna(x) else ""
        )
        es_hoy_mask = (df_todas["fecha"].astype(str) == hoy_str) & (df_todas["clasificacion"] != "Superada")
        grupo_hoy = df_todas[es_hoy_mask]
        resto = df_todas[~es_hoy_mask]
        grupo_agendada = resto[resto["clasificacion"] == "Agendada"]
        grupo_pendiente = resto[resto["clasificacion"] == "Pendiente"]
        grupo_superada = resto[resto["clasificacion"] == "Superada"]
    else:
        grupo_hoy = grupo_agendada = grupo_pendiente = grupo_superada = df_todas

    st.markdown("---")
    st.caption("Las tareas que pertenecen a un proyecto también aparecen acá (columna \"Proyecto\"), además de dentro de su proyecto más abajo.")
    st.markdown("### ⭐ Hoy")
    id_click = mostrar_tabla(grupo_hoy, "tabla_hoy", mostrar_columna_proyecto=True) or id_click

    st.markdown("### 🔵 Programadas")
    id_click = mostrar_tabla(grupo_agendada, "tabla_agendada", mostrar_columna_proyecto=True) or id_click

    st.markdown("### 🔴 Pendientes")
    id_click = mostrar_tabla(grupo_pendiente, "tabla_pendiente", mostrar_columna_proyecto=True) or id_click

    st.markdown("### 🟢 Superadas")
    id_click = mostrar_tabla(grupo_superada, "tabla_superada", mostrar_columna_proyecto=True) or id_click

    st.markdown("### 📁 Proyectos")
    if proyectos_df.empty:
        st.caption("Todavía no creaste ningún proyecto (podés hacerlo desde la barra lateral).")
    else:
        for _, proyecto in proyectos_df.iterrows():
            subtareas = df[df["proyecto_id"] == proyecto["id"]] if not df.empty else pd.DataFrame()
            if not subtareas.empty:
                subtareas = subtareas.copy()
                subtareas["clasificacion"] = subtareas.apply(lambda r: clasificar_tarea(r, ahora), axis=1)
                promedio = subtareas["avance"].mean()
            else:
                promedio = 0

            if promedio >= 100:
                color = "🟢"
            elif promedio > 0:
                color = "🟡"
            else:
                color = "⚪"

            with st.expander(f"{color} {proyecto['nombre']} — {promedio:.0f}% completado ({len(subtareas)} subtareas)"):
                if proyecto.get("notas"):
                    st.caption(proyecto["notas"])
                if subtareas.empty:
                    st.caption("Sin subtareas todavía. Creá una tarea nueva y asignale este proyecto.")
                else:
                    id_click = mostrar_tabla(subtareas, f"tabla_proy_{proyecto['id']}") or id_click
                if st.button("🗑️ Eliminar proyecto", key=f"del_proy_{proyecto['id']}"):
                    eliminar_proyecto(proyecto["id"])
                    st.rerun()

    if id_click:
        st.markdown("---")
        st.markdown("### ✏️ Editar tarea seleccionada")
        panel_edicion(id_click, df, proyectos_df, ahora)

# ==========================================
# RESUMEN EN FORMATO CALENDARIO (siempre al final)
# ==========================================
st.markdown("---")
st.markdown("## 🗓️ Calendario del mes")
mostrar_calendario_mes(df, ahora)
