"""El paquete contextual falla cerrado. Los ejemplos no son los clips del lote B."""

from pathlib import Path
from types import SimpleNamespace

import paquete_contextual
import prueba_privada
from video_editor import CaptionCue


def _cuts():
    # 4 s, silencio de 2.5 s, otros 4 s. Offset de fuente = 10.
    return [(10.0, 14.0), (16.5, 20.5)]


def _words():
    return [
        CaptionCue("antes", 8.0, 8.4),
        CaptionCue("entra", 10.2, 10.6),
        CaptionCue("mira", 12.0, 12.4),
        CaptionCue("pausa", 15.0, 15.3),
        CaptionCue("sigue", 17.0, 17.4),
        CaptionCue("despues", 21.0, 21.4),
        CaptionCue("fuera", 30.0, 30.3),
    ]


def _package(**overrides):
    data = dict(
        cortes=_cuts(),
        palabras=_words(),
        duracion_fuente=24.0,
        identidad={"fuente": {"estado": "presente", "sha256": "abc", "bytes": 3}},
    )
    data.update(overrides)
    return paquete_contextual.construir_paquete(**data)


def test_el_mapa_separa_silencio_y_offset():
    info = paquete_contextual.mapa_cortes(_cuts())
    assert info["offset_fuente"] == 10.0
    assert info["duracion_corte"] == 8.0
    assert info["silencios"] == [{
        "inicio_fuente": 14.0, "fin_fuente": 16.5, "duracion": 2.5, "quitado_del_render": True,
    }]
    assert paquete_contextual.map_cut_to_source(_cuts(), 0.0) == 10.0
    assert paquete_contextual.map_cut_to_source(_cuts(), 4.0) == 16.5
    assert paquete_contextual.map_source_to_cut(_cuts(), 14.0) == 4.0
    assert paquete_contextual.map_source_to_cut(_cuts(), 16.5) == 4.0
    assert paquete_contextual.map_cut_to_source(_cuts(), 7.95) == 20.45
    assert paquete_contextual.map_cut_to_source(_cuts(), 8.1) is None
    assert paquete_contextual.map_source_to_cut(_cuts(), 15.0) is None
    assert round(paquete_contextual.map_source_to_cut(_cuts(), 17.0), 3) == 4.5


def test_palabra_y_fotograma_desalineados_no_se_citan():
    package = _package()
    classes = {item["texto"]: item["clase"] for group in ("pre", "inside", "post", "silencio_quitado", "fuera_de_fuente") for item in package["palabras"][group]}
    assert classes == {
        "antes": "pre",
        "entra": "inside",
        "mira": "inside",
        "pausa": "silencio_quitado",
        "sigue": "inside",
        "despues": "post",
        "fuera": "fuera_de_fuente",
    }
    entra = next(item for item in package["palabras"]["inside"] if item["texto"] == "entra")
    frame_fin = package["fotogramas"][2]["reloj_corte"]
    assert paquete_contextual.cita_admisible(package, entra["i"], entra["t0_corte"]) is True
    assert paquete_contextual.cita_admisible(package, entra["i"], frame_fin) is False


def test_corte_o_palabra_fuera_de_la_fuente_se_abstiene():
    package = _package(duracion_fuente=12.0)
    assert package["corte_fuera_de_fuente"] is True
    assert "fuente" in paquete_contextual.motivo_de_bloqueo(package)


def test_sin_contexto_original_se_abstiene_y_no_usa_el_ass():
    moment = SimpleNamespace(start=10.0, cuts=_cuts(), cues=[CaptionCue("subtitulo", 0.2, 0.5)], source_words=None)
    package = paquete_contextual.paquete_de_momento(moment, Path("fuente.mp4"), duracion_fuente=24.0)
    assert package["palabras"]["estado"] == "missing"
    assert package["ass_es_asr_original"] is False
    assert "ASR" in paquete_contextual.motivo_de_bloqueo(package)


def test_un_payoff_solo_fuera_del_corte_no_se_admite():
    words = [CaptionCue("entra", 10.2, 10.6), CaptionCue("gana", 21.0, 21.4)]
    package = _package(palabras=words)
    payoff = next(item for item in package["palabras"]["post"] if item["texto"] == "gana")
    assert paquete_contextual.cita_admisible(package, payoff["i"], 21.0) is False
    assert package["cierre_declarable"] is False
    assert "no cierra" in package["limitacion"]


def test_la_receta_del_bench_es_la_del_adaptador():
    assert prueba_privada.plan_contextual(_cuts()) == paquete_contextual.frame_plan(_cuts())
    plan = paquete_contextual.frame_plan(_cuts())
    assert [item["indice"] for item in plan] == [0, 1, 2]
    assert [item["reloj_fuente"] for item in plan] == [10.0, 16.5, 20.45]
    assert all(item["ancho"] == 320 and item["materializado"] is False for item in plan)
    assert plan[0]["seek"] == "10.000"


def test_sin_medicion_de_backend_el_presupuesto_queda_desconocido():
    package = _package()
    assert package["presupuesto"]["estado"] == "desconocido"
    assert package["presupuesto"]["tokens_entrada"] is None
    assert paquete_contextual.motivo_de_bloqueo(package).startswith("abstención: presupuesto")
    plan, reason = paquete_contextual.preparar_llamada(package, {"prompt_eval_count": 100, "origen": "estimado"})
    assert plan is None and "presupuesto" in reason
    measured = {
        "origen": "backend",
        "prompt_eval_count": 100,
        "num_ctx": paquete_contextual.NUM_CTX,
        "num_predict": paquete_contextual.NUM_PREDICT,
    }
    plan, reason = paquete_contextual.preparar_llamada(package, measured)
    assert reason == ""
    assert plan == paquete_contextual.frame_plan(_cuts())
    too_big = dict(measured, prompt_eval_count=paquete_contextual.NUM_CTX)
    plan, reason = paquete_contextual.preparar_llamada(package, too_big)
    assert plan is None and "presupuesto" in reason


def test_el_adaptador_no_llama_aunque_el_paquete_este_medido(tmp_path):
    called = []

    class Cliente:
        def complete(self, prompt, images):
            called.append(1)

    moment = SimpleNamespace(
        start=10.0, cuts=_cuts(), cues=[], source_words=_words(), segment=None,
    )
    video = tmp_path / "fuente.mp4"
    video.write_bytes(b"fuente")
    medicion = {
        "origen": "backend",
        "prompt_eval_count": 100,
        "num_ctx": paquete_contextual.NUM_CTX,
        "num_predict": paquete_contextual.NUM_PREDICT,
    }
    verdict = prueba_privada._contextual_view(
        moment, video, Cliente(), tmp_path / "frames", medicion=medicion, duracion_fuente=24.0,
    )
    assert called == []
    assert verdict["cliente"] == "no_llamado"
    assert verdict["decision"] == "abstain"
    assert verdict["paquete"]["ensayo_hecho"] is False
    assert "no está hecha" in verdict["motivo"]


def test_un_filtro_con_silencio_recupera_el_mapa_sin_inventar_palabras(tmp_path):
    folder = tmp_path / "cand"
    folder.mkdir()
    (folder / "render_filter.txt").write_text(
        "[0:v]trim=start=10.000:end=14.000,setpts=PTS-STARTPTS[v0_0];\n"
        "[0:a]atrim=start=10.000:end=14.000,asetpts=PTS-STARTPTS[a0_0];\n"
        "[0:v]trim=start=16.500:end=20.500,setpts=PTS-STARTPTS[v0_1];\n"
        "[0:a]atrim=start=16.500:end=20.500,asetpts=PTS-STARTPTS[a0_1];\n",
        encoding="utf-8",
    )
    (folder / "manifest.json").write_text(
        '{"video": {"sha256": "no-importa"}, "tramo": {"inicio_origen": 10.0, "duracion_final": 8.0, '
        '"segmento": {"end": 20.5, "first_word": 1, "last_word": 4}}}',
        encoding="utf-8",
    )
    (folder / "subtitulos.ass").write_text("Dialogue: subtítulo que no es el ASR\n", encoding="utf-8")
    source = tmp_path / "cache.mp4"
    source.write_bytes(b"mp4")
    seen = paquete_contextual.observar_candidato(folder, source, duracion_fuente=24.0)
    assert seen["mapa"]["silencios"][0]["duracion"] == 2.5
    assert seen["palabras"]["estado"] == "missing"
    assert seen["ass"]["usado_como_asr"] is False
    assert seen["fotogramas"] == paquete_contextual.frame_plan(_cuts())
    assert seen["cliente"] == "no_llamado"
    assert seen["ensayo_hecho"] is False
