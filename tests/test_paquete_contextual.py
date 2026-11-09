"""El paquete contextual falla cerrado. Los ejemplos no son los clips del lote B."""

import json
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
    assert plan is None and "correlación" in reason
    con_prueba = _package()
    con_prueba["truncado"] = 0
    plan, reason = paquete_contextual.preparar_llamada(con_prueba, measured)
    assert plan is None and "correlación" in reason
    too_big = dict(measured, prompt_eval_count=paquete_contextual.NUM_CTX)
    plan, reason = paquete_contextual.preparar_llamada(con_prueba, too_big)
    assert plan is None and "presupuesto" in reason
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
    assert "trunc" in verdict["motivo"]


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


def _trace(count, **extra):
    data = {
        "raw": "",
        "prompt_eval_count": count,
        "eval_count": 1,
        "done_reason": "length",
        "error": None,
        "num_ctx": paquete_contextual.NUM_CTX,
        "num_predict_enviado": 1,
    }
    data.update(extra)
    return data


def test_la_entrada_del_preflight_es_la_del_paquete_y_deja_fuera_el_contexto():
    package = _package()
    coverage = paquete_contextual.cobertura_entrada(package)
    texts = [item["texto"] for item in coverage["palabras"]]
    assert texts == ["entra", "mira", "sigue"]
    assert "despues" not in texts and "fuera" not in texts
    import juez_contextual
    prompt = juez_contextual.prompt_for(coverage)
    huella = paquete_contextual.huella_entrada(prompt, juez_contextual.OUTPUT_SCHEMA, ["a", "b", "c"], 5120)
    body = juez_contextual.build_request("modelo", prompt, ["img"], 5120, 0)
    sent = juez_contextual.cuerpo_preflight(body)
    assert juez_contextual.entrada_identica(body, sent)
    sent["messages"][0]["images"] = ["otra"]
    assert juez_contextual.entrada_identica(body, sent) is False
    assert huella != paquete_contextual.huella_entrada(prompt, juez_contextual.OUTPUT_SCHEMA, ["a", "b", "d"], 5120)


def test_el_preflight_se_cierra_sin_correlacion_de_esta_llamada():
    package = _package()
    inst = {"huella": "h", "cuerpo_sha256": "cuerpo", "campos": {"prompt": "p"}}
    base = {
        "raw": "",
        "prompt_eval_count": 4799,
        "eval_count": 1,
        "done_reason": "length",
        "error": None,
        "num_ctx": paquete_contextual.NUM_CTX,
        "num_predict_enviado": 1,
        "sesion_id": "s1",
        "modelo": "modelo",
        "digest": "digest",
        "inicio_ms": 0,
        "fin_ms": 20,
        "cuerpo_sha256": "cuerpo",
        "bitacora": [{
            "sesion_id": "s1", "modelo": "modelo", "digest": "digest",
            "n_tokens": 4799, "truncated": 0, "t_ms": 10,
        }],
    }
    ok = paquete_contextual.aplicar_preflight(package, base, inst)
    assert ok["preflight"]["aplicada"] is True
    assert ok["correlacion"]["valida"] is True
    sin_bitacora = dict(base, bitacora=[])
    cerrado = paquete_contextual.aplicar_preflight(package, sin_bitacora, inst)
    assert cerrado["preflight"]["aplicada"] is False
    assert "truncated" not in sin_bitacora
    viejo = dict(base, bitacora=[{
        "sesion_id": "otra", "modelo": "modelo", "digest": "digest",
        "n_tokens": 100, "truncated": 0, "t_ms": 10,
    }])
    assert paquete_contextual.aplicar_preflight(package, viejo, inst)["preflight"]["aplicada"] is False
    discordante = dict(base, bitacora=[{
        "sesion_id": "s1", "modelo": "modelo", "digest": "digest",
        "n_tokens": 100, "truncated": 0, "t_ms": 10,
    }])
    assert paquete_contextual.aplicar_preflight(package, discordante, inst)["preflight"]["aplicada"] is False
    tardio = dict(base, bitacora=[{**base["bitacora"][0], "t_ms": 500}])
    assert paquete_contextual.aplicar_preflight(package, tardio, inst)["preflight"]["aplicada"] is False
    otro_modelo = dict(base, bitacora=[{**base["bitacora"][0], "digest": "otro"}])
    assert paquete_contextual.aplicar_preflight(package, otro_modelo, inst)["preflight"]["aplicada"] is False
    marcado = dict(base, bitacora=[{**base["bitacora"][0], "truncated": 1}])
    assert paquete_contextual.aplicar_preflight(package, marcado, inst)["preflight"]["aplicada"] is False
    misma_etiqueta = dict(base, cuerpo_sha256="otra")
    assert paquete_contextual.aplicar_preflight(package, misma_etiqueta, {"huella": "h", "cuerpo_sha256": "h"})["preflight"]["aplicada"] is False
    assert paquete_contextual.truncados_en_log("") == []


def test_el_preflight_real_no_conserva_el_texto(monkeypatch):
    import juez_contextual

    class Respuesta:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "message": {"content": '{"decision": "accept"}'},
                "done_reason": "length",
                "prompt_eval_count": 80,
                "eval_count": 1,
            }).encode()

    monkeypatch.setattr(juez_contextual.urllib.request, "urlopen", lambda *_a, **_k: Respuesta())
    trace = juez_contextual.OllamaVision("http://127.0.0.1:9", "modelo").preflight("prompt", ["aW1n"])
    assert trace["raw"] == ""
    assert trace["prompt_eval_count"] == 80
    assert trace["contenido_descartado"] is True
    assert "accept" not in json.dumps(trace)


def test_la_vista_mide_el_mismo_paquete_y_juzga_una_vez(tmp_path):
    import hashlib
    import subprocess
    import juez_contextual
    from video_editor import _ffmpeg_exe

    video = tmp_path / "fuente.mp4"
    subprocess.run(
        [_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:d=10",
         "-t", "10", str(video)],
        check=True,
    )
    seen = {}

    class Transporte:
        modelo = "modelo"
        digest = "digest-fijo"

        def preflight(self, prompt, images, cuerpo):
            seen["prompt"] = prompt
            seen["n"] = len(images)
            enviado = juez_contextual.cuerpo_preflight(cuerpo)
            if getattr(self, "mutar", False):
                enviado = dict(enviado)
                enviado["messages"] = [{"role": "user", "content": "prompt cambiado", "images": images}]
            raw = json.dumps(enviado, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            count = 80
            return {
                "raw": "",
                "prompt_eval_count": count,
                "eval_count": 1,
                "done_reason": "length",
                "error": None,
                "num_ctx": 5120,
                "num_predict_enviado": 1,
                "sesion_id": "s1",
                "modelo": self.modelo,
                "digest": self.digest,
                "inicio_ms": 0,
                "fin_ms": 20,
                "cuerpo_sha256": hashlib.sha256(raw).hexdigest(),
                "bitacora": [{
                    "sesion_id": "s1", "modelo": self.modelo, "digest": self.digest,
                    "n_tokens": count, "truncated": 0, "t_ms": 10,
                }],
            }

        def complete(self, prompt, images):
            seen["juzgo"] = seen.get("juzgo", 0) + 1
            seen["prompt_juicio"] = prompt
            return {
                "raw": json.dumps({"decision": "abstain", "motivo": "stub", "evidencias": []}),
                "done_reason": "stop",
                "error": None,
                "prompt_eval_count": 80,
                "eval_count": 12,
                "num_ctx": 5120,
                "num_predict": 220,
            }

    words = [
        CaptionCue("antes", 0.1, 0.3),
        CaptionCue("entra", 1.0, 1.4),
        CaptionCue("mira", 3.0, 3.4),
        CaptionCue("despues", 9.2, 9.5),
    ]
    moment = SimpleNamespace(start=0.5, cuts=[(0.5, 8.5)], source_words=words, segment=None, cues=[])
    cliente = Transporte()
    verdict = prueba_privada._contextual_view(moment, video, cliente, tmp_path / "frames", duracion_fuente=10.0)
    assert seen["n"] == 3
    assert "entra" in seen["prompt"] and "despues" not in seen["prompt"]
    assert seen["juzgo"] == 1
    assert "despues" not in seen["prompt_juicio"]
    assert verdict["cliente"] == "juicio"
    assert verdict["decision"] == "abstain"
    assert verdict["llamadas_semanticas"] == 1
    assert verdict["paquete"]["presupuesto"]["tokens_entrada"] == 80
    assert verdict["paquete"]["correlacion"]["valida"] is True
    assert verdict["cobertura_vista_por_el_modelo"]["contexto_completo"] is False
    cambiado = dict(verdict["paquete"])
    dentro = [dict(item) for item in cambiado["palabras"]["inside"]]
    dentro[0] = dict(dentro[0], texto="CAMBIADA")
    cambiado["palabras"] = dict(cambiado["palabras"], inside=dentro)
    otra = prueba_privada._juzgar_una_vez(cambiado, Transporte(), juez_contextual)
    assert otra["llamadas_semanticas"] == 0
    assert "ya no es" in otra["motivo"]
    assert [item["reloj_fuente"] for item in verdict["paquete"]["fotogramas"]] == [0.5, 4.5, 8.45]
    assert all(item["sha256"] and item["materializado"] for item in verdict["paquete"]["fotogramas"])


def test_un_corte_fuera_de_la_fuente_no_llega_al_preflight(tmp_path):
    called = []

    class Medidor:
        def preflight(self, prompt, images):
            called.append(1)
            return _trace(100)

    moment = SimpleNamespace(start=10.0, cuts=_cuts(), source_words=_words(), segment=None, cues=[])
    verdict = prueba_privada._contextual_view(
        moment, tmp_path / "no.mp4", Medidor(), tmp_path / "frames", duracion_fuente=12.0,
    )
    assert called == []
    assert verdict["cliente"] == "no_llamado"
    assert "fuente" in verdict["motivo"]


def test_el_log_no_inventa_truncated_y_la_abstencion_textual_no_llama():
    assert paquete_contextual.truncados_en_log("") == []
    assert paquete_contextual.truncados_en_log("sin ese renglón") == []
    linea = "slot release: stop processing: n_tokens = 4799, truncated = 0"
    assert paquete_contextual.truncados_en_log(linea) == [{"n_tokens": 4799, "truncated": 0}]
    assert paquete_contextual.truncados_en_log("stop processing: n_tokens = 10, truncated = 1")[0]["truncated"] == 1
    called = []
    result = prueba_privada.seguir_si_textual_acepto(
        {"aceptado": False, "decision": "abstencion", "abstenciones": ["se unió otra unidad"]},
        lambda: called.append(1),
    )
    assert called == []
    assert result["llamadas_semanticas"] == 0
    assert result["cliente"] == "no_llamado"
