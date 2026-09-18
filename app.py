import hashlib
import io
import json
import os
import time
from datetime import datetime, timedelta
import pandas as pd
import plotly.express as px
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

# ==========================================
# PWA: hacer la app instalable (icono en pantalla de inicio)
# ==========================================
# Requiere: .streamlit/config.toml con enableStaticServing = true, y los
# archivos static/manifest.json, static/icon-192.png, static/icon-512.png
# y static/service-worker.js incluidos en el repositorio.
#
# Nota: en Streamlit Community Cloud hay un problema conocido de la
# plataforma por el cual, al instalar la app, el nombre y el icono que
# aparecen en la pantalla de inicio a veces siguen mostrando "Streamlit"
# en vez de los propios, aunque el manifest este bien cargado (bug
# reportado por varios usuarios, sin resolver hasta la fecha). El boton
# de instalar y el modo "app sin barra del navegador" igual funcionan.
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
# tocando nada en pantalla.
st_autorefresh(interval=20_000, limit=None, key="autorefresh_alertas")

# ==========================================
# TRANSCRIPCIÓN DE VOZ A TEXTO
# ==========================================
IDIOMA_RECONOCIMIENTO = "es-AR"  # cambialo a "es-ES", "es-MX", etc. si preferís


def transcribir_audio(audio_bytes: bytes, idioma: str = IDIOMA_RECONOCIMIENTO):
    """
    Convierte el audio grabado (WAV, tal como lo entrega st.audio_input)
    a texto usando el reconocedor gratuito de Google (requiere internet,
    no necesita API key).

    Devuelve una tupla (texto, error). Si la transcripción fue exitosa,
    'error' es None. Si falló, 'texto' es None y 'error' trae el motivo
    para mostrarlo al usuario.
    """
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
# FUNCIONES DE BASE DE DATOS
# ==========================================


def cargar_tareas():
    try:
        res = (
            supabase.from_("tareas")
            .select("*")
            .order("fecha", desc=False)
            .order("hora", desc=False)
            .execute()
        )
        return pd.DataFrame(res.data)
    except Exception as e:
        st.error(f"Error al cargar tareas: {e}")
        return pd.DataFrame()


def agregar_tarea(titulo, categoria, responsable, fecha, hora, notas):
    """Toda tarea nueva arranca en 0% / Pendiente. Se clasifica sola como
    'Agendada' en el resumen si la fecha/hora quedó en el futuro."""
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
    }
    supabase.from_("tareas").insert(data).execute()


def actualizar_tarea_completa(id_tarea, avance, notas, fecha, hora):
    """Guarda edicion de una tarea existente: % cumplimiento, notas y
    fecha/hora (para reprogramarla). El estado se deriva solo del avance:
    100% -> Superada, lo que sea menos -> Pendiente (y si la fecha/hora
    quedo en el futuro, el resumen la va a mostrar como 'Agendada').
    Reactiva la alerta de 15 minutos por si la reprogramaste."""
    estado = "Superado" if avance >= 100 else "Pendiente"
    data = {
        "estado": estado,
        "avance": avance,
        "notas": notas,
        "fecha": str(fecha),
        "hora": str(hora),
        "alertado": False,
    }
    supabase.from_("tareas").update(data).eq("id", id_tarea).execute()


def marcar_alertado(id_tarea):
    supabase.from_("tareas").update({"alertado": True}).eq("id", id_tarea).execute()


def eliminar_tarea(id_tarea):
    supabase.from_("tareas").delete().eq("id", id_tarea).execute()


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
    """Boton real (no de Streamlit) para pedir permiso de notificaciones y
    'desbloquear' el audio del navegador. Es obligatorio un clic humano:
    los navegadores no dejan reproducir sonido ni pedir notificaciones
    mediante codigo sin que la persona interactue primero."""
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
        const boton = document.getElementById('btn-activar-alertas');
        const estado = document.getElementById('estado-alertas');
        boton.addEventListener('click', function () {
            const p = window.parent;
            try {
                if (!p.__appAudioCtx) {
                    p.__appAudioCtx = new (p.AudioContext || p.webkitAudioContext)();
                }
                p.__appAudioCtx.resume();
            } catch (e) {}
            if (p.Notification && p.Notification.requestPermission) {
                p.Notification.requestPermission().then(function (perm) {
                    estado.innerText = perm === 'granted'
                        ? '✅ Notificaciones y sonido activados'
                        : '⚠️ Notificaciones bloqueadas (el sonido igual quedó activo)';
                });
            } else {
                estado.innerText = '✅ Sonido activado';
            }
            boton.innerText = '🔔 Alertas activadas en este dispositivo';
            boton.disabled = true;
            boton.style.opacity = '0.7';
        });
        </script>
        """,
        height=55,
    )


# ==========================================
# ESTADO DEL FORMULARIO (sidebar)
# ==========================================
def _hora_por_defecto():
    return (datetime.now() + timedelta(minutes=30)).time()


def _valores_por_defecto_formulario():
    return {
        "titulo_input": "",
        "notas_input": "",
        "responsable_input": "Yo",
        "categoria_input": "ENRESP",
        "fecha_input": datetime.now().date(),
        "hora_input": _hora_por_defecto(),
        "ultimo_audio_hash": "",
    }


# Si el envio anterior pidio limpiar el formulario, lo hacemos ACA, antes
# de crear ningun widget con esas claves (Streamlit no permite tocar
# st.session_state de un widget despues de haberlo creado en la misma
# corrida del script).
if st.session_state.get("_reset_formulario", False):
    for clave, valor in _valores_por_defecto_formulario().items():
        st.session_state[clave] = valor
    st.session_state["_reset_formulario"] = False

# Completa las claves que todavia no existan (primera vez que corre la app).
for clave, valor in _valores_por_defecto_formulario().items():
    if clave not in st.session_state:
        st.session_state[clave] = valor

# ==========================================
# BARRA LATERAL (ENTRADA DE DATOS)
# ==========================================
st.sidebar.title("📌 Agendar Tarea")

modo_ingreso = st.sidebar.radio(
    "Método de entrada:", ["✍️ Formulario Manual", "🎙️ Grabar Audio de Voz"]
)

if modo_ingreso == "🎙️ Grabar Audio de Voz":
    st.sidebar.markdown("**Presiona el micrófono y habla:**")
    audio_file = st.sidebar.audio_input("Grabar nota")

    if audio_file is not None:
        audio_bytes = audio_file.getvalue()
        # Huella digital del audio: solo transcribimos si es una grabacion
        # NUEVA. Sin esto, Streamlit vuelve a correr este bloque en cada
        # rerun (por ejemplo al tipear en otro campo) y pisaria el titulo
        # o las notas que ya hayas editado a mano.
        audio_hash = hashlib.md5(audio_bytes).hexdigest()

        if st.session_state.ultimo_audio_hash != audio_hash:
            with st.sidebar:
                with st.spinner("🎧 Transcribiendo audio..."):
                    texto, error = transcribir_audio(audio_bytes)

            st.session_state.ultimo_audio_hash = audio_hash

            if texto:
                st.session_state.titulo_input = texto[:80]
                st.session_state.notas_input = texto
                st.sidebar.success(f"✅ Transcripto: \u201c{texto[:60]}{'...' if len(texto) > 60 else ''}\u201d")
            else:
                st.sidebar.error(f"⚠️ No se pudo transcribir el audio: {error}")

with st.sidebar.form("form_tarea", clear_on_submit=False):
    titulo = st.text_input("Título de la Tarea", key="titulo_input")
    categoria = st.selectbox("Categoría", ["ENRESP", "EXTERNO"], key="categoria_input")
    responsable = st.text_input("Responsable", key="responsable_input")
    fecha = st.date_input("Fecha", key="fecha_input")
    hora = st.time_input("Hora de inicio", key="hora_input")
    notas = st.text_area("Notas / Minuta inicial", key="notas_input")

    submitted = st.form_submit_button(
        "➕ Agendar Tarea", use_container_width=True
    )

    if submitted:
        if titulo.strip() != "":
            agregar_tarea(titulo, categoria, responsable, fecha, hora, notas)

            st.toast("🎉 ¡Tarea agregada con éxito!", icon="✅")
            st.sidebar.success("✅ Tarea registrada en la base de datos.")

            # Pedimos el reset para la PROXIMA corrida del script (ver
            # bloque de arriba), no ahora mismo.
            st.session_state["_reset_formulario"] = True

            time.sleep(1)
            st.rerun()
        else:
            st.sidebar.error("⚠️ Debes colocar un título a la tarea.")

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
ahora = datetime.now()
fecha_hoy_str = ahora.strftime("%Y-%m-%d")

if not df.empty:
    df["clasificacion"] = df.apply(lambda r: clasificar_tarea(r, ahora), axis=1)

verificar_alertas(df, ahora)

vista = st.radio(
    "Modo de Visualización:",
    ["📊 Tablero Completo", "📱 Lista Resumen (Fácil Celular)"],
    horizontal=True,
)

st.caption(f"📅 Día actual: **{ahora.strftime('%d/%m/%Y')}**")

if not df.empty:
    if vista == "📱 Lista Resumen (Fácil Celular)":
        st.markdown("### 📋 Resumen Rápido de Tareas")

        df_resumen = df.copy()
        df_resumen["⭐ Hoy"] = df_resumen["fecha"].apply(
            lambda x: "⭐ ¡HOY!" if str(x) == fecha_hoy_str else "📅 Programada"
        )

        df_display = df_resumen[
            [
                "⭐ Hoy",
                "clasificacion",
                "fecha",
                "hora",
                "categoria",
                "titulo",
                "responsable",
                "avance",
                "notas",
            ]
        ].copy()

        df_display.columns = [
            "Prioridad",
            "Clasificación",
            "Fecha",
            "Hora",
            "Categoría",
            "Título",
            "Responsable",
            "Avance %",
            "Notas",
        ]

        st.dataframe(
            df_display,
            use_container_width=True,
            hide_index=True,
        )

    else:
        # ---------- MÉTRICAS ----------
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Tareas", len(df))
        c2.metric("⭐ Hoy", len(df[df["fecha"] == fecha_hoy_str]))
        c3.metric("🟢 Superadas", int((df["clasificacion"] == "Superada").sum()))
        c4.metric("🔵 Agendadas", int((df["clasificacion"] == "Agendada").sum()))
        c5.metric("🔴 Pendientes", int((df["clasificacion"] == "Pendiente").sum()))

        st.markdown("---")

        # ---------- GRÁFICO INTERACTIVO ----------
        st.markdown("#### 📊 Carga laboral y cumplimiento de objetivos")

        conteo = (
            df.groupby(["clasificacion", "categoria"])
            .size()
            .reset_index(name="cantidad")
        )
        orden_clasificacion = ["Pendiente", "Agendada", "Superada"]

        fig = px.bar(
            conteo,
            x="clasificacion",
            y="cantidad",
            color="categoria",
            category_orders={"clasificacion": orden_clasificacion},
            barmode="stack",
            custom_data=["categoria"],
            color_discrete_map={"ENRESP": "#4f6df5", "EXTERNO": "#f2994a"},
            labels={
                "clasificacion": "Clasificación",
                "cantidad": "Cantidad de tareas",
                "categoria": "Categoría",
            },
        )
        fig.update_layout(
            height=360,
            margin=dict(t=10, b=10),
            clickmode="event+select",
            legend_title_text="Categoría",
        )

        evento_grafico = st.plotly_chart(
            fig,
            key="grafico_clasificacion",
            on_select="rerun",
            selection_mode="points",
            use_container_width=True,
        )

        clas_click = None
        cat_click = None
        if evento_grafico and evento_grafico["selection"]["points"]:
            punto = evento_grafico["selection"]["points"][0]
            clas_click = punto.get("x")
            datos_extra = punto.get("customdata")
            cat_click = datos_extra[0] if datos_extra else None

        if clas_click:
            titulo_filtro = f"📌 Tareas — {clas_click}"
            if cat_click:
                titulo_filtro += f" / {cat_click}"
            st.markdown(f"##### {titulo_filtro}")

            df_click = df[df["clasificacion"] == clas_click]
            if cat_click:
                df_click = df_click[df_click["categoria"] == cat_click]

            st.dataframe(
                df_click[
                    ["fecha", "hora", "categoria", "titulo", "responsable", "avance", "notas"]
                ],
                use_container_width=True,
                hide_index=True,
            )
            st.caption("Hacé clic en otra barra para cambiar el filtro.")

        st.markdown("---")

        # ---------- LISTADO POR CATEGORÍA ----------
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            filtro_cat = st.multiselect(
                "Categorías:",
                ["ENRESP", "EXTERNO"],
                default=["ENRESP", "EXTERNO"],
            )
        with col_f2:
            filtro_clas = st.multiselect(
                "Clasificación:",
                ["Pendiente", "Agendada", "Superada"],
                default=["Pendiente", "Agendada", "Superada"],
            )

        df_filtered = df[
            (df["categoria"].isin(filtro_cat))
            & (df["clasificacion"].isin(filtro_clas))
        ]

        for cat in ["ENRESP", "EXTERNO"]:
            if cat in filtro_cat:
                df_cat = df_filtered[df_filtered["categoria"] == cat]
                st.subheader(f"📂 Categoría: {cat}")

                if not df_cat.empty:
                    for idx, row in df_cat.iterrows():
                        es_hoy = str(row["fecha"]) == fecha_hoy_str
                        clas = row["clasificacion"]
                        badge_texto = {
                            "Superada": "🟢 Superada",
                            "Agendada": "🔵 Agendada",
                            "Pendiente": "🔴 Pendiente",
                        }[clas]

                        with st.container():
                            col1, col2, col3, col4 = st.columns([3, 2, 1.6, 1.3])

                            with col1:
                                if es_hoy:
                                    st.markdown(
                                        f"⭐ **{row['titulo']}** <span style='color:#2563eb;'>(¡HOY!)</span>",
                                        unsafe_allow_html=True,
                                    )
                                else:
                                    st.markdown(f"**{row['titulo']}**")
                                st.caption(f"👤 {row['responsable']}")

                            with col2:
                                st.caption(f"📅 {row['fecha']}  ⏰ {str(row['hora'])[:5]}")
                                st.markdown(badge_texto)

                            with col3:
                                st.progress(min(int(row["avance"]), 100) / 100)
                                st.caption(f"{int(row['avance'])}% cumplido")

                            with col4:
                                with st.popover("✏️ Editar", use_container_width=True):
                                    st.markdown(f"**{row['titulo']}**")
                                    st.caption(f"Clasificación actual: {badge_texto}")

                                    try:
                                        fecha_actual = pd.to_datetime(row["fecha"]).date()
                                    except Exception:
                                        fecha_actual = ahora.date()
                                    try:
                                        hora_actual = datetime.strptime(
                                            str(row["hora"])[:5], "%H:%M"
                                        ).time()
                                    except Exception:
                                        hora_actual = ahora.time()

                                    nueva_fecha = st.date_input(
                                        "Fecha", value=fecha_actual, key=f"fecha_{row['id']}"
                                    )
                                    nueva_hora = st.time_input(
                                        "Hora", value=hora_actual, key=f"hora_{row['id']}"
                                    )
                                    pct = st.slider(
                                        "% Cumplimiento",
                                        0, 100, int(row["avance"]),
                                        key=f"pct_{row['id']}",
                                    )
                                    nuevas_notas = st.text_area(
                                        "Notas / Acuerdos",
                                        value=row["notas"] if row["notas"] else "",
                                        key=f"nt_{row['id']}",
                                    )

                                    if pct >= 100:
                                        st.success("Al guardar queda como **Superada**.")
                                    else:
                                        fh_nueva = datetime.combine(nueva_fecha, nueva_hora)
                                        if fh_nueva >= ahora:
                                            st.info("Al guardar queda como **Agendada** (fecha/hora futura).")
                                        else:
                                            st.warning(
                                                "Al guardar queda como **Pendiente** (sin fecha futura). "
                                                "Elegí una fecha/hora futura para agendarla."
                                            )

                                    bc1, bc2 = st.columns(2)
                                    with bc1:
                                        if st.button("💾 Guardar", key=f"sv_{row['id']}", use_container_width=True):
                                            actualizar_tarea_completa(
                                                row["id"], pct, nuevas_notas, nueva_fecha, nueva_hora
                                            )
                                            for k in (
                                                f"fecha_{row['id']}", f"hora_{row['id']}",
                                                f"pct_{row['id']}", f"nt_{row['id']}",
                                            ):
                                                st.session_state.pop(k, None)
                                            st.toast("Guardado correctamente")
                                            st.rerun()
                                    with bc2:
                                        if st.button("🗑️ Eliminar", key=f"del_{row['id']}", use_container_width=True):
                                            eliminar_tarea(row["id"])
                                            st.rerun()

                        st.divider()
                else:
                    st.caption("Sin tareas en esta categoría.")
else:
    st.info("No hay tareas registradas en la base de datos.")
