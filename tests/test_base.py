"""Contrato de la base de curiosidades: sin red y sin FFmpeg."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import clip_judge
import clipazos
from base.configuracion import calidad_de, estilo_de, load_config
from base.render import Pieza, Visual, plan_render, repartir
from base.subtitulos import caja_texto, escribir_subtitulos, eventos_palabra, sincronizar, validar_estilo
from base.texto import hashtag, texto_publicacion
from base.transcripcion import opciones_transcripcion
from video_editor import CaptionCue


def test_la_config_activa_no_trae_el_formato_de_streamers():
    config = load_config()
    for clave in ("twitch", "kick", "youtube", "rights", "seleccion", "radar", "asr", "semanal"):
        assert clave not in config, clave
    assert config["formato"] == "curiosidades"
    assert config["render"]["loudness_lufs"] == -14
    assert config["render"]["ancho"] == 1080 and config["render"]["alto"] == 1920
    assert config["subtitulos"]["resalte_bgr"] == "00FFFF"
    assert config["publication"]["db_path"] == "data/publicaciones.sqlite3"
    assert config["subida_youtube"]["activa"] is False


def test_los_parametros_de_calidad_salen_de_la_config_y_no_de_un_literal():
    calidad = calidad_de({"render": {"crf": 17, "fps": 30, "preset": "slow", "loudness_lufs": -14}})
    assert (calidad.crf, calidad.fps, calidad.preset, calidad.loudness_lufs) == (17, 30, "slow", -14)
    assert (calidad.ancho, calidad.alto) == (1080, 1920)
    raro = calidad_de({"render": {"crf": 80, "fps": -1, "codec_video": ""}})
    assert raro.crf == 18 and raro.fps == 30 and raro.codec_video == "libx264"


def test_el_estilo_de_subtitulos_es_configurable():
    estilo = estilo_de({
        "subtitulos": {
            "fuente": "Fuente Prueba",
            "tamano": 64,
            "resalte_bgr": "0000EE",
            "margen_inferior": 500,
            "margen_superior": 200,
            "color_bgr": "no-es-hex",
        }
    })
    assert estilo.fuente == "Fuente Prueba"
    assert estilo.tamano == 64
    assert estilo.resalte_bgr == "0000EE"
    assert estilo.margen_inferior == 500
    assert estilo.color_bgr == "FFFFFF"


def test_la_zona_segura_cabe_con_los_valores_de_partida():
    estilo = estilo_de(None)
    validar_estilo(estilo, 1920)
    y0, y1 = caja_texto(
        estilo.alineacion, estilo.margen_inferior, 1920, estilo.tamano, 1,
        estilo.contorno, estilo.sombra, estilo.escala_palabra_activa / 100,
    )
    assert y0 >= estilo.margen_superior
    assert y1 <= 1920 - estilo.margen_inferior + 1e-6


def test_un_margen_inferior_demasiado_bajo_no_se_acepta():
    estilo = estilo_de({"subtitulos": {"margen_inferior": 1700, "tamano": 200, "margen_superior": 400}})
    with pytest.raises(ValueError, match="zona superior"):
        validar_estilo(estilo, 1920)


def test_el_ass_toma_fuente_color_y_margen_de_la_config(tmp_path):
    config = {
        "subtitulos": {
            "fuente": "Fuente Prueba",
            "tamano": 60,
            "resalte_bgr": "0000EE",
            "margen_inferior": 500,
            "margen_superior": 200,
            "escala_palabra_activa": 120,
        }
    }
    destino = escribir_subtitulos(
        [CaptionCue("hola", 0.0, 0.4), CaptionCue("mundo", 0.4, 0.9)],
        tmp_path / "piezas.ass",
        config,
        titulo="¿Sabías que?",
        duracion=3,
    )
    texto = destino.read_text(encoding="utf-8-sig")
    assert "Style: Caption,Fuente Prueba,60," in texto
    assert "&H000000EE&" in texto or "0000EE" in texto
    assert ",2,90,90,500,1" in texto
    assert "CaptionSplit" not in texto
    assert "facecam" not in texto.casefold()
    assert texto.count("Dialogue: 0,") == 2
    assert "¿Sabías que?" in texto


def test_los_eventos_son_una_palabra_activa_y_no_rellenan_el_silencio():
    estilo = estilo_de(None)
    eventos = eventos_palabra(
        [CaptionCue("uno", 0.0, 0.3), CaptionCue("dos", 0.8, 1.1)],
        estilo,
    )
    assert len(eventos) == 2
    assert eventos[0][1] <= 0.3 + 1e-9 or eventos[0][1] <= eventos[1][0]
    assert eventos[1][0] >= 0.8 - 1e-9
    assert estilo.resalte_bgr in eventos[0][2]
    assert "UNO" in eventos[0][2]
    assert eventos[0][1] < eventos[1][0]
    assert "DOS" in eventos[1][2]


def test_sincronizar_no_deja_intervalos_solapados():
    cues = sincronizar([
        CaptionCue("a", 0.0, 0.5),
        CaptionCue("b", 0.4, 0.6),
        CaptionCue("c", 0.6, 0.6),
    ])
    for anterior, siguiente in zip(cues, cues[1:]):
        assert anterior.end <= siguiente.start
        assert anterior.end > anterior.start


def test_el_plan_de_render_es_vertical_y_normaliza_a_menos_14(tmp_path):
    voz = tmp_path / "voz.wav"
    foto = tmp_path / "plano.png"
    clip = tmp_path / "plano.mp4"
    voz.write_bytes(b"a")
    foto.write_bytes(b"b")
    clip.write_bytes(b"c")
    config = {"render": {"crf": 16, "preset": "slow", "fps": 30, "loudness_lufs": -14, "bitrate_audio": "192k"}}
    pieza = Pieza(
        visuales=(Visual(foto, duracion=2, tipo="imagen"), Visual(clip, tipo="video", inicio=1.0)),
        voz=voz,
        palabras=(CaptionCue("dato", 0.2, 0.6),),
        duracion=5,
        titulo="Dato",
    )
    plan = plan_render(pieza, tmp_path / "out" / "short.mp4", config)
    assert "-filter_complex_script" not in plan.comando
    assert "-/filter_complex" in plan.comando
    assert plan.comando[plan.comando.index("-crf") + 1] == "16"
    assert plan.comando[plan.comando.index("-preset") + 1] == "slow"
    assert plan.comando[plan.comando.index("-r") + 1] == "30"
    assert plan.comando[plan.comando.index("-b:a") + 1] == "192k"
    assert "-map" in plan.comando and "[vout]" in plan.comando and "[aout]" in plan.comando
    texto = plan.filtro_texto
    assert "loudnorm=I=-14" in texto
    assert "scale=1080:1920" in texto
    assert "concat=n=2" in texto
    assert "-loop" in plan.comando and "-stream_loop" in plan.comando
    assert "trim=start=1.000" in texto
    for prohibido in ("facecam", "CaptionSplit", "SPLIT", "chat"):
        assert prohibido not in texto
    assert plan.ass.is_file() and "Dialogue: 0," in plan.ass.read_text(encoding="utf-8-sig")


def test_repartir_falla_sin_visuales_o_si_se_pasan_de_tiempo():
    with pytest.raises(ValueError, match="al menos un visual"):
        repartir((), 4)
    foto = Visual("a.png", duracion=5, tipo="imagen")
    with pytest.raises(ValueError, match="superan"):
        repartir((foto,), 3)


def test_la_transcripcion_no_manda_hotwords_de_streamer():
    opciones = opciones_transcripcion("es")
    assert opciones["word_timestamps"] is True
    assert opciones["language"] == "es"
    assert "hotwords" not in opciones and "prefix" not in opciones


def test_el_texto_de_publicacion_no_inventa_creditos_de_streamer():
    post = texto_publicacion("Un gato", "Fuente: ejemplo.", ["curiosidades", "#curiosidades", "dato curioso"])
    assert post["titulo"] == "Un gato"
    assert post["hashtags"] == ["#curiosidades", "#datocurioso"]
    assert hashtag("Dato curioso") == "#datocurioso"
    assert "streamer" not in post["descripcion"]


def test_el_juez_acepta_un_tramo_generico_y_rechaza_el_vacio():
    palabras = [
        CaptionCue("El", 0.1, 0.4),
        CaptionCue("gato", 0.4, 0.7),
        CaptionCue("duerme", 0.7, 1.1),
        CaptionCue("sobre", 1.1, 1.5),
        CaptionCue("la", 1.5, 1.8),
        CaptionCue("manta", 1.8, 2.2),
        CaptionCue("roja.", 2.2, 2.8),
    ]
    tramo = SimpleNamespace(
        start=0.0, end=4.0, peak=1.1, length=4.0,
        complete_start=True, complete_end=True, crosses_units=False, sentences=2,
    )
    config = {"seleccion": {"segundos_min": 1, "segundos_max": 20, "juez": {"min_palabras": 6}}}
    verdict = clip_judge.judge(palabras, tramo, config=config)
    assert verdict.accepted, verdict.reasons
    vacio = clip_judge.judge([], tramo, config=config)
    assert not vacio.accepted


def test_importar_la_base_no_carga_modulos_archivados():
    import base
    import tiktok_export
    import youtube_upload

    assert base.render_pieza is not None
    assert clipazos.parse_args(["--dry-run"]).dry_run is True
    assert clipazos.parse_args(["--forzar"]).forzar is True
    prohibidos = {
        "narrative_segments", "juez_contextual", "paquete_contextual",
        "prueba_privada", "rights",
    }
    assert not (prohibidos & set(sys.modules))
    assert tiktok_export.approval({"qa": {"ok": False}})[0] is False
    assert youtube_upload.final_checks is not None


def test_los_stubs_de_curiosidades_no_hacen_el_formato():
    import curiosidades.guion as guion
    import curiosidades.pipeline as pipeline
    import curiosidades.verificacion as verificacion
    import curiosidades.visuales as visuales
    import curiosidades.voz as voz

    with pytest.raises(NotImplementedError):
        guion.escribir_guion("gatos")
    with pytest.raises(NotImplementedError):
        verificacion.verificar({})
    with pytest.raises(NotImplementedError):
        voz.sintetizar({}, "voz.mp3")
    with pytest.raises(NotImplementedError):
        visuales.buscar({})
    with pytest.raises(NotImplementedError):
        pipeline.ejecutar("gatos")
