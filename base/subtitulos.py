"""Subtítulos ASS palabra a palabra, con la zona segura en la configuración.

Sirve igual para marcas de Edge TTS (WordBoundary) y para palabras de Whisper:
las dos son un texto con inicio y fin en segundos. No conoce cámaras, chats ni
layouts de directo.
"""

from __future__ import annotations

from pathlib import Path

from video_editor import CaptionCue, _ass_clock, _ass_word, sync_cues

from base.configuracion import CalidadRender, EstiloSubtitulo, calidad_de, estilo_de

# El mismo tipo que ya usa el editor. Un WordBoundary se convierte en uno de estos.
CuePalabra = CaptionCue


def sincronizar(palabras: list[CaptionCue]) -> list[CaptionCue]:
    """Recorta cada palabra para que no pise el intervalo de la siguiente."""
    return sync_cues(list(palabras))


def caja_texto(
    alineacion: int,
    margen_v: int,
    alto: int,
    tamano: int,
    lineas: int,
    contorno: int,
    sombra: int,
    escala: float,
) -> tuple[float, float]:
    """Borde superior e inferior del renglón, en píxeles del PlayRes.

    Alineación 8: ``margen_v`` es el borde superior (desde arriba).
    Alineación 2: ``margen_v`` es la distancia desde abajo hasta la base del texto.
    La escala (1.3 si la palabra entra al 130 %) crece el bloque hacia fuera del ancla.
    """
    if alineacion not in (2, 8):
        raise ValueError(f"alineación {alineacion} no tiene zona segura definida (usa 2 o 8)")
    alto_caja = (tamano * max(1, lineas) + contorno + sombra) * max(float(escala), 1.0)
    if alineacion == 8:
        y0 = float(margen_v)
        return y0, y0 + alto_caja
    y1 = float(alto - margen_v)
    return y1 - alto_caja, y1


def validar_estilo(estilo: EstiloSubtitulo, alto: int) -> None:
    """Falla si el subtítulo o el título se salen de la zona segura."""
    escala = estilo.escala_palabra_activa / 100.0
    y0, y1 = caja_texto(
        estilo.alineacion, estilo.margen_inferior, alto, estilo.tamano, 1,
        estilo.contorno, estilo.sombra, escala,
    )
    if y0 < estilo.margen_superior:
        raise ValueError(
            f"el subtítulo invade la zona superior (y={y0:.0f}, mínimo {estilo.margen_superior})"
        )
    limite_inferior = alto - estilo.margen_inferior
    if y1 > limite_inferior + 1e-6:
        raise ValueError(
            f"el subtítulo invade la zona inferior de la UI (y={y1:.0f}, máximo {limite_inferior:.0f})"
        )
    t0, t1 = caja_texto(
        estilo.alineacion_titulo, estilo.margen_superior, alto, estilo.tamano_titulo, 1,
        estilo.contorno_titulo, estilo.sombra_titulo, 1.0,
    )
    if t0 < 0 or t1 > limite_inferior:
        raise ValueError(
            f"el título no cabe en la zona segura (y {t0:.0f}-{t1:.0f}, hueco hasta {limite_inferior:.0f})"
        )


def _fragmento(palabra: str, activa: bool, estilo: EstiloSubtitulo) -> str:
    etiqueta = _ass_word(palabra)
    if not etiqueta:
        return ""
    if not activa:
        return etiqueta
    escala = estilo.escala_palabra_activa
    ms = estilo.fundido_entrada_ms
    return (
        f"{{\\c&H{estilo.resalte_bgr}&\\fscx{escala}\\fscy{escala}\\t(0,{ms},\\fscx100\\fscy100)}}"
        f"{etiqueta}"
        f"{{\\c&H{estilo.color_bgr}&\\fscx100\\fscy100}}"
    )


def _linea(palabras: list[str], activa: int, estilo: EstiloSubtitulo) -> str:
    trozos = [_fragmento(palabra, index == activa, estilo) for index, palabra in enumerate(palabras)]
    trozos = [trozo for trozo in trozos if trozo]
    if not trozos:
        return ""
    return (
        f"{{\\q2\\fad({estilo.fundido_entrada_ms},{estilo.fundido_salida_ms})}}"
        + " ".join(trozos)
    )


def _grupos(cues: list[CaptionCue], por_linea: int) -> dict[int, list[int]]:
    grupos: list[list[int]] = []
    cubo: list[int] = []
    for index, cue in enumerate(cues):
        cubo.append(index)
        if len(cubo) >= por_linea or cue.text.rstrip().endswith((".", "!", "?", "…", ";", ":")):
            grupos.append(cubo)
            cubo = []
    if cubo:
        grupos.append(cubo)
    return {index: grupo for grupo in grupos for index in grupo}


def eventos_palabra(
    palabras: list[CaptionCue], estilo: EstiloSubtitulo | None = None
) -> list[tuple[float, float, str]]:
    """Un evento por palabra, solo mientras se dice.

    La línea enseña hasta ``palabras_por_linea`` palabras y resalta la activa.
    En el silencio no se deja la frase puesta ni se adelanta la siguiente.
    """
    estilo = estilo or estilo_de(None)
    cues = sincronizar(palabras)
    grupos = _grupos(cues, max(1, estilo.palabras_por_linea))
    eventos: list[tuple[float, float, str]] = []
    for index, cue in enumerate(cues):
        grupo = grupos.get(index, [index])
        fin = max(cue.end, cue.start + estilo.duracion_minima_palabra)
        if index + 1 < len(cues):
            fin = min(fin, cues[index + 1].start)
        if fin <= cue.start:
            fin = cue.start + estilo.duracion_minima_palabra
        texto = _linea([cues[item].text for item in grupo], grupo.index(index), estilo)
        if not texto:
            continue
        eventos.append((cue.start, fin, texto))
    return eventos


def _estilo_ass(
    nombre: str,
    fuente: str,
    tamano: int,
    color_bgr: str,
    contorno: int,
    sombra: int,
    alineacion: int,
    margen_l: int,
    margen_v: int,
    estilo: EstiloSubtitulo,
) -> str:
    return (
        f"Style: {nombre},{fuente},{tamano},"
        f"&H00{color_bgr},&H00{estilo.resalte_bgr},&H00000000,&H00000000,"
        f"{estilo.negrita},0,0,0,100,100,{estilo.espaciado},0,1,{contorno},{sombra},"
        f"{alineacion},{margen_l},{margen_l},{margen_v},1"
    )


def escapar_ass(texto: str) -> str:
    """Quita llaves y barras, que en ASS abren un override."""
    return texto.replace("\\", "").replace("{", "").replace("}", "").replace("\n", " ").strip()


def escribir_subtitulos(
    palabras: list[CaptionCue],
    destino: Path,
    config: dict | None = None,
    *,
    titulo: str = "",
    duracion: float | None = None,
) -> Path:
    """Escribe el .ass del vertical. ``destino`` es la ruta del propio .ass."""
    estilo = estilo_de(config)
    calidad = calidad_de(config)
    validar_estilo(estilo, calidad.alto)
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    eventos = eventos_palabra(list(palabras), estilo)
    lineas = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {calidad.ancho}",
        f"PlayResY: {calidad.alto}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        _estilo_ass(
            "Title", estilo.fuente, estilo.tamano_titulo, estilo.color_bgr,
            estilo.contorno_titulo, estilo.sombra_titulo, estilo.alineacion_titulo,
            estilo.margen_lateral, estilo.margen_superior, estilo,
        ),
        _estilo_ass(
            "Caption", estilo.fuente, estilo.tamano, estilo.color_bgr,
            estilo.contorno, estilo.sombra, estilo.alineacion,
            estilo.margen_lateral, estilo.margen_inferior, estilo,
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    titulo_limpio = escapar_ass(titulo)
    if titulo_limpio and estilo.titulo_segundos > 0:
        tope = duracion if duracion is not None else estilo.titulo_segundos
        fin = min(estilo.titulo_segundos, max(0.0, tope))
        if fin > 0:
            lineas.append(
                f"Dialogue: 1,{_ass_clock(0)},{_ass_clock(fin)},Title,,0,0,0,,"
                f"{{\\fad({estilo.fundido_entrada_ms},{estilo.fundido_salida_ms})}}{titulo_limpio}"
            )
    for inicio, fin, texto in eventos:
        lineas.append(
            f"Dialogue: 0,{_ass_clock(inicio)},{_ass_clock(fin)},Caption,,0,0,0,,{texto}"
        )
    destino.write_text("\n".join(lineas) + "\n", encoding="utf-8-sig")
    return destino


def ruta_subtitulos(video: Path) -> Path:
    """El .ass que acompaña a un MP4 (mismo nombre, otra extensión)."""
    return Path(video).with_suffix(".ass")
