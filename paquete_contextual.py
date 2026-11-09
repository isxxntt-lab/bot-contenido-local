"""Paquete único que comparten el ensayo y el adaptador privado.

No llama a un modelo, no transcribe y no renderiza. Si falta el ASR original,
el mapa de cortes o una medición real del presupuesto, el paquete queda en
abstención. Tres fotogramas no declaran un cierre.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

RECIPE_ID = "fuente-3-reloj-corte-v1"
FRAME_COUNT = 3
FRAME_WIDTH = 320
END_MARGIN = 0.05
NUM_CTX = 5120
NUM_PREDICT = 220
LIMITACION = (
    "Tres fotogramas no prueban una acción ni el audio. "
    "Un desenlace que no cae dentro del corte no cierra este recorte."
)

_VIDEO_TRIM = re.compile(r"\[(\d+):v\]trim=start=([0-9.]+):end=([0-9.]+)")
_AUDIO_TRIM = re.compile(r"\[(\d+):a\]atrim=start=([0-9.]+):end=([0-9.]+)")


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def identidad_archivo(path: Path | None, declarado: str | None = None) -> dict:
    if path is None or not path.is_file():
        return {"estado": "missing", "ruta": None if path is None else str(path), "sha256": None, "bytes": None}
    digest = sha256_file(path)
    record = {"estado": "presente", "ruta": str(path), "sha256": digest, "bytes": path.stat().st_size}
    if declarado is not None:
        record["coincide_con_manifiesto"] = declarado == digest
    return record


def mapa_cortes(cuts: list[tuple[float, float]] | None) -> dict:
    """Mapa fuente→reloj del corte. No inventa un tramo continuo si no hay cortes."""
    if not cuts:
        return {
            "estado": "missing",
            "offset_fuente": None,
            "duracion_corte": None,
            "piezas": None,
            "silencios": None,
        }
    cursor = 0.0
    piezas = []
    silencios = []
    for index, (left, right) in enumerate(cuts):
        if right < left:
            return {
                "estado": "invalido",
                "offset_fuente": None,
                "duracion_corte": None,
                "piezas": None,
                "silencios": None,
            }
        span = right - left
        piezas.append({
            "indice": index,
            "fuente": [round(left, 3), round(right, 3)],
            "reloj_corte": [round(cursor, 3), round(cursor + span, 3)],
        })
        if index:
            previous = cuts[index - 1][1]
            if left > previous + 1e-6:
                silencios.append({
                    "inicio_fuente": round(previous, 3),
                    "fin_fuente": round(left, 3),
                    "duracion": round(left - previous, 3),
                    "quitado_del_render": True,
                })
        cursor += span
    return {
        "estado": "explicito",
        "offset_fuente": round(cuts[0][0], 3),
        "duracion_corte": round(cursor, 3),
        "piezas": piezas,
        "silencios": silencios,
    }


def map_cut_to_source(cuts: list[tuple[float, float]], cut_time: float) -> float | None:
    """Pasa del reloj del corte al de la fuente.

    Cada pieza cubre su intervalo por la izquierda. El instante exacto del
    empalme pertenece a la pieza siguiente. Pasado el final no se recorta al borde.
    """
    if not cuts or cut_time < -1e-9:
        return None
    cursor = 0.0
    last = len(cuts) - 1
    for index, (left, right) in enumerate(cuts):
        span = right - left
        end = cursor + span
        if index == last:
            if cursor - 1e-9 <= cut_time <= end + 1e-6:
                return left + (cut_time - cursor)
        elif cursor - 1e-9 <= cut_time < end - 1e-9:
            return left + (cut_time - cursor)
        cursor = end
    return None


def map_source_to_cut(cuts: list[tuple[float, float]], source_time: float) -> float | None:
    """None si el instante cae antes, después o en un silencio quitado."""
    if not cuts:
        return None
    cursor = 0.0
    for left, right in cuts:
        if left - 1e-6 <= source_time <= right + 1e-6:
            return cursor + (source_time - left)
        if source_time < left:
            return None
        cursor += right - left
    return None


def frame_plan(cuts: list[tuple[float, float]] | None) -> list[dict]:
    """Tres seeks sobre la fuente: inicio, medio y fin−0,05 s del reloj del corte."""
    info = mapa_cortes(cuts)
    if info["estado"] != "explicito":
        return []
    length = float(info["duracion_corte"])
    if length <= END_MARGIN:
        return []
    times = [0.0, length / 2.0, length - END_MARGIN]
    plan = []
    for index, cut_time in enumerate(times):
        source = map_cut_to_source(cuts or [], cut_time)
        inside = source is not None and map_source_to_cut(cuts or [], source) is not None
        plan.append({
            "indice": index,
            "reloj_corte": round(cut_time, 3),
            "reloj_fuente": None if source is None else round(source, 3),
            "ancho": FRAME_WIDTH,
            "alto": "proporcional",
            "seek": None if source is None else f"{source:.3f}",
            "dentro_del_corte": inside,
            "materializado": False,
            "archivo": None,
            "sha256": None,
        })
    return plan


def _clase_palabra(start: float, end: float, cuts: list[tuple[float, float]], duracion: float | None) -> str:
    if start < -1e-6 or (duracion is not None and start >= duracion - 1e-6):
        return "fuera_de_fuente"
    if not cuts:
        return "sin_mapa"
    overlapped = []
    for left, right in cuts:
        if start < right and end > left:
            overlapped.append((left, right))
    if not overlapped:
        if end <= cuts[0][0] + 1e-6:
            return "pre"
        if start >= cuts[-1][1] - 1e-6:
            return "post"
        return "silencio_quitado"
    fully = any(left - 1e-6 <= start and end <= right + 1e-6 for left, right in overlapped)
    if fully:
        return "inside"
    return "limite"


def _palabra(index: int, word, cuts: list[tuple[float, float]], clase: str) -> dict:
    cut_start = map_source_to_cut(cuts, float(word.start)) if cuts else None
    cut_end = map_source_to_cut(cuts, float(word.end)) if cuts else None
    return {
        "i": index,
        "texto": str(word.text),
        "t0_fuente": round(float(word.start), 3),
        "t1_fuente": round(float(word.end), 3),
        "t0_corte": None if cut_start is None else round(cut_start, 3),
        "t1_corte": None if cut_end is None else round(cut_end, 3),
        "clase": clase,
    }


def clasificar_palabras(words, cuts: list[tuple[float, float]] | None, duracion: float | None) -> dict:
    if words is None:
        return {
            "estado": "missing",
            "reloj": None,
            "pre": None,
            "inside": None,
            "post": None,
            "silencio_quitado": None,
            "limite": None,
            "fuera_de_fuente": None,
            "inside_n": 0,
            "motivo": "no se conservó el ASR original fechado; el ASS del corto no lo sustituye",
        }
    grouped = {key: [] for key in ("pre", "inside", "post", "silencio_quitado", "limite", "fuera_de_fuente", "sin_mapa")}
    for index, word in enumerate(words):
        clase = _clase_palabra(float(word.start), float(word.end), cuts or [], duracion)
        grouped[clase].append(_palabra(index, word, cuts or [], clase))
    return {
        "estado": "presente",
        "reloj": "fuente",
        "pre": grouped["pre"],
        "inside": grouped["inside"],
        "post": grouped["post"],
        "silencio_quitado": grouped["silencio_quitado"],
        "limite": grouped["limite"],
        "fuera_de_fuente": grouped["fuera_de_fuente"],
        "inside_n": len(grouped["inside"]),
        "motivo": None,
    }


def presupuesto(medicion: dict | None = None) -> dict:
    """El techo 5120/220 es el cupo congelado, no una cuenta de tokens."""
    data = {
        "num_ctx": NUM_CTX,
        "num_predict": NUM_PREDICT,
        "margen_salida": NUM_PREDICT,
        "tokens_entrada": None,
        "estado": "desconocido",
        "cabe": None,
        "motivo": "no se acredita sin una medición del backend sobre este paquete; no se estima",
    }
    if not medicion or medicion.get("origen") != "backend":
        return data
    count = medicion.get("prompt_eval_count")
    if not isinstance(count, int) or count < 0:
        return data
    if medicion.get("num_ctx") != NUM_CTX or medicion.get("num_predict") != NUM_PREDICT:
        data["motivo"] = "la medición no corresponde a este presupuesto congelado"
        return data
    data["tokens_entrada"] = count
    data["estado"] = "medido"
    data["cabe"] = count + NUM_PREDICT <= NUM_CTX
    data["motivo"] = "medido por el backend" if data["cabe"] else "la entrada medida no deja el margen de salida"
    return data


def construir_paquete(
    *,
    cortes: list[tuple[float, float]] | None,
    palabras,
    duracion_fuente: float | None,
    identidad: dict | None = None,
    medicion: dict | None = None,
    intervalo_candidato: dict | None = None,
) -> dict:
    """Arma el paquete. No rellena palabras ni tiempos que no se le hayan pasado."""
    mapa = mapa_cortes(cortes)
    palabras_info = clasificar_palabras(palabras, cortes, duracion_fuente)
    plan = frame_plan(cortes)
    missing = []
    if palabras is None:
        missing.append("asr_original")
    if mapa["estado"] != "explicito":
        missing.append("mapa_cortes")
    if duracion_fuente is None:
        missing.append("duracion_fuente")
    fuente = (identidad or {}).get("fuente") or {"estado": "missing"}
    if fuente.get("estado") != "presente":
        missing.append("hash_fuente")
    fuera = False
    if duracion_fuente is not None and cortes:
        fuera = any(left < -0.02 or right > duracion_fuente + 0.02 for left, right in cortes)
    if fuera:
        missing.append("corte_dentro_de_fuente")
    return {
        "ensayo_hecho": False,
        "cierre_declarable": False,
        "receta": {
            "id": RECIPE_ID,
            "puntos": FRAME_COUNT,
            "ancho": FRAME_WIDTH,
            "margen_fin": END_MARGIN,
            "materializada": False,
        },
        "limitacion": LIMITACION,
        "identidad": identidad or {},
        "intervalo_candidato": intervalo_candidato,
        "mapa": mapa,
        "palabras": palabras_info,
        "fotogramas": plan,
        "presupuesto": presupuesto(medicion),
        "missing": missing,
        "corte_fuera_de_fuente": fuera,
        "ass_es_asr_original": False,
        "limites_cobertura": _limites(palabras_info),
        "contexto_completo": False,
        "tres_fotogramas_prueban_accion": False,
    }


def _limites(palabras: dict) -> dict:
    def n(key):
        value = palabras.get(key)
        return None if value is None else len(value)
    return {
        "pre": n("pre"),
        "dentro": palabras.get("inside_n"),
        "post": n("post"),
        "limite": n("limite"),
        "silencio_quitado": n("silencio_quitado"),
        "texto_en_la_peticion": "solo_palabras_dentro",
        "contexto_completo": False,
    }


_PARADA = re.compile(
    r"slot\s+release:\s+id\s+(\d+)\s+\|\s+task\s+(\d+)\s+\|\s+stop processing:\s+n_tokens = (\d+), truncated = (\d+)"
)
_POST_CHAT = re.compile(r'POST\s+"/api/chat"')


def paradas_en_log(texto: str) -> list[dict]:
    """Solo renglones con slot y task. Sin esos ids no hay atribución."""
    found = []
    for slot, task, tokens, flag in _PARADA.findall(texto or ""):
        found.append({
            "slot": int(slot),
            "task": int(task),
            "n_tokens": int(tokens),
            "truncated": int(flag),
        })
    return found


def truncados_en_log(texto: str) -> list[dict]:
    """Compatibilidad: mismas paradas, sin inventar un 0 si el renglón no trae ids."""
    return paradas_en_log(texto)


def correlacionar_observacion(respuesta: dict) -> dict:
    """El buffer del log no tiene que igualar al conteo de la API. Sí tiene que ser el de esta petición."""
    vacio = {
        "valida": False,
        "truncated": None,
        "prompt_eval_count": None,
        "eval_count": None,
        "buffer_n_tokens": None,
        "motivo": "no hay observación de esta llamada",
    }
    if not isinstance(respuesta, dict):
        return vacio
    count = respuesta.get("prompt_eval_count")
    if not isinstance(count, int):
        return {**vacio, "motivo": "la respuesta no trae prompt_eval_count"}
    if not respuesta.get("modelo") or not respuesta.get("digest"):
        return {**vacio, "motivo": "no hay modelo o digest observado en el runtime"}
    if not isinstance(respuesta.get("log_previo"), str) or not isinstance(respuesta.get("log_nuevo"), str):
        return {**vacio, "motivo": "falta el log previo o el nuevo; no se puede excluir un renglón viejo"}
    previas = {(item["slot"], item["task"]) for item in paradas_en_log(respuesta["log_previo"])}
    nuevas = [item for item in paradas_en_log(respuesta["log_nuevo"]) if (item["slot"], item["task"]) not in previas]
    posts = len(_POST_CHAT.findall(respuesta["log_nuevo"]))
    base = {
        "prompt_eval_count": count,
        "eval_count": respuesta.get("eval_count") if isinstance(respuesta.get("eval_count"), int) else None,
    }
    if len(nuevas) != 1 or posts != 1:
        return {**vacio, **base, "motivo": "el log nuevo no ata una única parada y un único POST a esta llamada"}
    linea = nuevas[0]
    return {
        "valida": linea["truncated"] == 0,
        "truncated": linea["truncated"],
        "n_tokens": linea["n_tokens"],
        "buffer_n_tokens": linea["n_tokens"],
        "slot": linea["slot"],
        "task": linea["task"],
        "modelo": respuesta["modelo"],
        "digest": respuesta["digest"],
        **base,
        "motivo": "parada nueva de esta llamada" if linea["truncated"] == 0 else f"truncated={linea['truncated']}",
    }


def cobertura_entrada(package: dict) -> dict | None:
    """Palabras de dentro, en el reloj del corte. Lo de fuera no entra en la petición."""
    words = package.get("palabras") or {}
    mapa = package.get("mapa") or {}
    if words.get("estado") != "presente" or mapa.get("estado") != "explicito":
        return None
    inside = []
    for item in words.get("inside") or []:
        if item.get("t0_corte") is None or item.get("t1_corte") is None:
            return None
        inside.append({
            "i": item["i"],
            "t0": item["t0_corte"],
            "t1": item["t1_corte"],
            "texto": item["texto"],
        })
    return {
        "inicio": 0.0,
        "fin": mapa["duracion_corte"],
        "palabras": inside,
        "fotogramas": [item["reloj_corte"] for item in package.get("fotogramas") or []],
    }


def huella_entrada(prompt: str, schema: dict, frame_shas: list[str], num_ctx: int) -> str:
    payload = {
        "prompt": prompt,
        "schema": schema,
        "frames": frame_shas,
        "num_ctx": num_ctx,
        "margen_salida": NUM_PREDICT,
        "receta": RECIPE_ID,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def instantanea_entrada(
    *,
    prompt: str,
    schema: dict,
    imagenes: list[bytes],
    modelo: str,
    digest: str,
    num_ctx: int,
    fuente_sha: str | None,
    mapa: dict,
    palabras_dentro: list,
) -> dict:
    """Huella de lo que se enviaría. Se recalcula al medir y otra vez al juzgar."""
    campos = {
        "prompt": prompt,
        "schema": schema,
        "imagenes": [hashlib.sha256(blob).hexdigest() for blob in imagenes],
        "modelo": modelo,
        "digest": digest,
        "num_ctx": num_ctx,
        "margen_salida": NUM_PREDICT,
        "fuente_sha256": fuente_sha,
        "mapa": mapa,
        "palabras_dentro": palabras_dentro,
        "contexto_completo": False,
        "tres_fotogramas_prueban_accion": False,
        "render_en_la_decision": False,
    }
    raw = json.dumps(campos, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"campos": campos, "huella": hashlib.sha256(raw).hexdigest()}


def aplicar_preflight(package: dict, trace: dict, instantanea_releida: dict) -> dict:
    """La medición vale si el transporte y una relectura independiente coinciden, y la bitácora ata el truncado."""
    updated = dict(package)
    info = {
        "aplicada": False,
        "contenido_descartado": not bool(trace.get("raw")),
        "num_predict_enviado": trace.get("num_predict_enviado"),
        "eval_count": trace.get("eval_count"),
        "done_reason": trace.get("done_reason"),
        "error": trace.get("error"),
        "motivo": "",
    }
    corr = correlacionar_observacion(trace)
    info["correlacion"] = corr
    if trace.get("raw"):
        info["motivo"] = "la salida no se descartó; no cuenta como medición ni como decisión"
    elif trace.get("error"):
        info["motivo"] = str(trace["error"])
    elif trace.get("num_predict_enviado") != 1:
        info["motivo"] = "el preflight no limitó la generación a un token descartado"
    elif not isinstance(trace.get("prompt_eval_count"), int):
        info["motivo"] = "el backend no devolvió prompt_eval_count"
    elif trace.get("num_ctx") != NUM_CTX:
        info["motivo"] = "la medición no usa el contexto congelado"
    elif not isinstance(instantanea_releida, dict) or not instantanea_releida.get("huella"):
        info["motivo"] = "no hay relectura independiente de la entrada"
    elif trace.get("cuerpo_bytes_sha256") != instantanea_releida.get("cuerpo_bytes_sha256"):
        info["motivo"] = "los bytes enviados no son los de la relectura"
    elif not corr["valida"]:
        info["motivo"] = corr["motivo"]
    else:
        updated["presupuesto"] = presupuesto({
            "origen": "backend",
            "prompt_eval_count": trace["prompt_eval_count"],
            "num_ctx": NUM_CTX,
            "num_predict": NUM_PREDICT,
        })
        updated["correlacion"] = corr
        updated["congelado"] = {
            "huella": instantanea_releida["huella"],
            "campos": instantanea_releida.get("campos"),
            "cuerpo_bytes_sha256": instantanea_releida.get("cuerpo_bytes_sha256"),
        }
        info["aplicada"] = updated["presupuesto"]["estado"] == "medido" and updated["presupuesto"]["cabe"] is True
        info["motivo"] = updated["presupuesto"]["motivo"] if info["aplicada"] else "la entrada medida no deja el margen de salida"
    updated["preflight"] = info
    updated["ensayo_hecho"] = False
    return updated


def motivo_de_bloqueo(package: dict) -> str:
    """Razón para no evaluar. Vacío solo si el paquete ya trae una medición que cabe."""
    if "asr_original" in package["missing"]:
        return "abstención: no está el ASR original fechado"
    if "mapa_cortes" in package["missing"]:
        return "abstención: no está el mapa fuente→corte"
    if "duracion_fuente" in package["missing"]:
        return "abstención: no se conoce la duración de la fuente"
    if package.get("corte_fuera_de_fuente"):
        return "abstención: el corte sale de la fuente disponible"
    frames = package.get("fotogramas") or []
    if len(frames) != FRAME_COUNT or any(not item["dentro_del_corte"] for item in frames):
        return "abstención: la receta no coloca los tres fotogramas dentro del corte"
    if package["palabras"]["inside_n"] <= 0:
        return "abstención: no hay palabras del ASR dentro del corte"
    pres = package["presupuesto"]
    if pres["estado"] != "medido" or pres["cabe"] is not True:
        return "abstención: presupuesto de entrada no verificado"
    corr = package.get("correlacion") or {}
    if corr.get("valida") is not True or corr.get("truncated") != 0:
        return "abstención: no hay correlación de no truncamiento con esta llamada"
    if corr.get("n_tokens") != pres.get("tokens_entrada"):
        return "abstención: el conteo de la bitácora no es el de la respuesta"
    return ""


def cita_admisible(package: dict, palabra_i: int, stamp: float) -> bool:
    """Una cita solo vale si la palabra está dentro y el tiempo cae en su intervalo del corte."""
    inside = package["palabras"].get("inside") or []
    word = next((item for item in inside if item["i"] == palabra_i), None)
    if word is None or word["t0_corte"] is None or word["t1_corte"] is None:
        return False
    return word["t0_corte"] - 0.05 <= stamp <= word["t1_corte"] + 0.05


def preparar_llamada(package: dict, medicion: dict | None = None) -> tuple[list[dict] | None, str]:
    """Devuelve el plan compartido solo cuando el bloqueo desaparece. No llama al modelo."""
    if medicion is not None:
        package = dict(package)
        package["presupuesto"] = presupuesto(medicion)
    reason = motivo_de_bloqueo(package)
    if reason:
        return None, reason
    return list(package["fotogramas"]), ""


def trims_de_filtro(text: str) -> dict:
    """Lee los trim ya escritos en un filter script. No calcula un corte nuevo."""
    video = [(float(start), float(end)) for _i, start, end in _VIDEO_TRIM.findall(text)]
    audio = [(float(start), float(end)) for _i, start, end in _AUDIO_TRIM.findall(text)]
    if not video:
        return {"estado": "missing", "video": None, "audio": None, "coinciden": False}
    same = [(round(a, 3), round(b, 3)) for a, b in video] == [(round(a, 3), round(b, 3)) for a, b in audio]
    return {"estado": "explicito", "video": video, "audio": audio, "coinciden": same and bool(audio)}


def paquete_de_momento(moment, fuente: Path | None = None, duracion_fuente: float | None = None, medicion: dict | None = None) -> dict:
    """Usa el ASR de origen del momento. Los subtítulos ya remontados no lo reemplazan."""
    words = getattr(moment, "source_words", None)
    cuts = list(moment.cuts) if getattr(moment, "cuts", None) else None
    identidad = {"fuente": identidad_archivo(fuente)} if fuente is not None else {"fuente": {"estado": "missing"}}
    intervalo = None
    segment = getattr(moment, "segment", None)
    if segment:
        intervalo = {
            "inicio": segment.get("start"),
            "fin": segment.get("end"),
            "first_word": segment.get("first_word"),
            "last_word": segment.get("last_word"),
        }
    package = construir_paquete(
        cortes=cuts,
        palabras=words,
        duracion_fuente=duracion_fuente,
        identidad=identidad,
        medicion=medicion,
        intervalo_candidato=intervalo,
    )
    start = getattr(moment, "start", None)
    if start is not None and cuts and abs(float(start) - cuts[0][0]) > 0.02:
        package["desajuste_inicio"] = {
            "inicio_momento": round(float(start), 3),
            "primer_corte": round(cuts[0][0], 3),
            "bloquea": False,
        }
    return package


def observar_candidato(
    carpeta: Path,
    cache: Path | None,
    *,
    duracion_fuente: float | None = None,
    duracion_render: float | None = None,
    duracion_rotulo: float | None = None,
    fotogramas_ajenos: list[dict] | None = None,
) -> dict:
    """Cobertura de un candidato ya exportado. No modifica sus archivos ni lee el ASS como ASR."""
    manifest_path = carpeta / "manifest.json"
    manifest = {}
    if manifest_path.is_file():
        import json
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    filtro = carpeta / "render_filter.txt"
    trims = trims_de_filtro(filtro.read_text(encoding="utf-8")) if filtro.is_file() else {"estado": "missing", "video": None, "audio": None, "coinciden": False}
    declarado = ((manifest.get("video") or {}).get("sha256"))
    identidad = {
        "fuente": identidad_archivo(cache),
        "render": identidad_archivo(carpeta / "render.mp4"),
        "rotulado": identidad_archivo(carpeta / "PRUEBA_NO_PUBLICAR.mp4", declarado),
    }
    tramo = manifest.get("tramo") or {}
    segmento = tramo.get("segmento") or {}
    package = construir_paquete(
        cortes=trims["video"] if trims["estado"] == "explicito" and trims["coinciden"] else None,
        palabras=None,
        duracion_fuente=duracion_fuente,
        identidad=identidad,
        intervalo_candidato={
            "manifiesto_inicio": tramo.get("inicio_origen"),
            "manifiesto_fin": segmento.get("end"),
            "manifiesto_duracion": tramo.get("duracion_final"),
            "first_word": segmento.get("first_word"),
            "last_word": segmento.get("last_word"),
            "indices_sin_lista": segmento.get("first_word") is not None,
        },
    )
    package["filtro"] = {
        "estado": trims["estado"],
        "audio_igual_que_video": trims["coinciden"],
        "ruta": str(filtro) if filtro.is_file() else None,
    }
    package["duraciones_medidas"] = {
        "fuente": duracion_fuente,
        "render": duracion_render,
        "rotulado": duracion_rotulo,
    }
    package["ass"] = {
        "presente": (carpeta / "subtitulos.ass").is_file(),
        "usado_como_asr": False,
    }
    package["fotogramas_no_canonicos"] = fotogramas_ajenos or []
    package["motivo_bloqueo"] = motivo_de_bloqueo(package)
    package["cliente"] = "no_llamado"
    return package
