"""Historia completa: misma intervención, duración tras el recorte y ventana de VOD."""

from __future__ import annotations

import numpy as np

import clipazos
import clip_judge
import narrative_segments as ns
from video_editor import CaptionCue


def palabras(frases: list[tuple[str, float]], inicio: float = 0.5) -> list[CaptionCue]:
    """Frases con una pausa previa. Cada palabra dura 0,32 s y se separa 0,08 s."""
    words: list[CaptionCue] = []
    cursor = inicio
    for text, pause in frases:
        cursor += pause
        tokens = text.split()
        for index, token in enumerate(tokens):
            words.append(CaptionCue(token, round(cursor, 3), round(cursor + 0.32, 3)))
            cursor += 0.40
            if index == len(tokens) - 1:
                cursor += 0.05
    return words


def historia(**extra):
    base = {
        "historia_activa": True,
        "historia_planteamiento": 8.0,
        "historia_objetivo": 32.0,
        "historia_minimo": 25.0,
        "historia_maximo": 50.0,
        "segundos_min": 25.0,
        "segundos_max": 50.0,
        "preparacion_segundos": 3.0,
        "remate_segundos": 2.0,
        "pausa_frase": 0.6,
        "pausa_unidad": 1.5,
        "margen_inicio": 0.15,
        "margen_final": 0.35,
        "silencio_corte": 1.0,
        "silencio_margen": 0.3,
        "borde_clip_segundos": 0.3,
    }
    base.update(extra)
    return base


def test_flag_apagado_no_cambia_el_cruce_de_unidades():
    words = palabras([
        ("Vale hoy empezamos la partida con calma y sin ninguna prisa en el directo.", 0.0),
        ("Cada uno saca su celular y espera el siguiente aviso antes de seguir.", 2.0),
    ])
    total = words[-1].end + 2
    peak = words[3].start
    off = ns.segment_for_peak(words, peak, historia(historia_activa=False, segundos_min=10, segundos_max=45), total)
    classic = ns.segment_for_peak(words, peak, {"segundos_min": 10, "segundos_max": 45, "pausa_unidad": 1.5}, total)
    assert off.crosses_units is classic.crosses_units
    assert abs(off.start - classic.start) < 0.05 and abs(off.end - classic.end) < 0.05


def test_una_intervencion_de_34s_queda_entre_25_y_40_con_planteamiento():
    frases = []
    texto = (
        "Vale chat os cuento con calma lo que acaba de pasar en esta partida final del jefe. "
        "Estaba tranquilo farmando en el centro cuando el rival aparece por detrás sin hacer ruido. "
        "Yo me giro tarde y el golpe me deja la vida al mínimo justo encima del pico. "
        "NOOO ahora sí que se viene el remate porque el equipo entero entra a defender la base. "
        "Al final sobrevivimos por un pelo y el chat no se lo cree de verdad."
    )
    # Una sola intervención: pausas cortas, pico hacia la mitad, unos 34 s de voz.
    chunks = [parte.strip() + "." for parte in texto.split(".") if parte.strip()]
    for index, chunk in enumerate(chunks):
        frases.append((chunk, 0.15 if index else 0.0))
    words = palabras(frases, inicio=0.4)
    assert 30 <= words[-1].end - words[0].start <= 40
    peak = next(word.start for word in words if word.text.startswith("NOOO"))
    segment = ns.segment_for_peak(words, peak, historia(), words[-1].end + 1)
    assert segment.crosses_units is False
    assert "historia corta" not in segment.notes
    assert 25 <= segment.length <= 40
    assert peak - segment.start >= 8


def test_once_segundos_mas_otra_intervencion_no_se_unen():
    words = palabras([
        ("Esta historia es corta y se queda en unos once segundos de una sola vez.", 0.0),
        ("Luego empieza otra charla distinta que no pertenece a este momento del clip.", 3.0),
    ])
    peak = words[2].start
    segment = ns.segment_for_peak(words, peak, historia(), words[-1].end + 1)
    assert segment.crosses_units is False
    assert segment.length < 20
    assert "historia corta" in segment.notes
    assert not any("otra unidad" in note for note in segment.notes)


def test_veintiseis_con_silencio_falla_despues_del_recorte(monkeypatch):
    monkeypatch.setattr(clipazos, "find_peak", lambda audio, cues, inicio=None, fin=None: cues[1].start)
    words = palabras([
        ("Planteo corto del momento.", 0.0),
        ("Remate después de un silencio largo que no es la misma idea.", 10.0),
    ], inicio=0.2)
    audio = np.zeros(int(26 * clipazos.SAMPLE_RATE), dtype=np.float32)
    config = {"seleccion": {"segundos_min": 25, "segundos_max": 50, "pausa_unidad": 12, "historia": {"activa": True}}}
    segment, length, _cues, _peak, _cuts, verdict = clipazos.select_segment(audio, words, config)
    assert length < 25
    assert not verdict.accepted
    assert any("historia corta" in motivo for motivo in verdict.reasons)
    assert "historia corta" in segment.notes


def test_quince_segundos_sin_vod_se_rechaza(monkeypatch):
    monkeypatch.setattr(clipazos, "find_peak", lambda audio, cues, inicio=None, fin=None: cues[2].start)
    words = palabras([
        ("Esto es un clip corto de quince segundos sin principio de verdad.", 0.0),
        ("Y el final también se queda a medias en la misma frase corta.", 0.2),
    ], inicio=0.3)
    assert words[-1].end < 15
    audio = np.zeros(int(15 * clipazos.SAMPLE_RATE), dtype=np.float32)
    _segment, length, _cues, _peak, _cuts, verdict = clipazos.select_segment(
        audio, words, {"seleccion": {"historia": {"activa": True}, "segundos_min": 25, "segundos_max": 50}}
    )
    assert length < 25 and not verdict.accepted
    assert any("historia corta" in motivo for motivo in verdict.reasons)


def test_ventana_de_vod_y_respaldo_al_clip(tmp_path, monkeypatch):
    clip = clipazos.Candidate(
        "Twitch", "Rubius", "corto", 1000, "https://clips.twitch.tv/Abc", "twitch-Abc",
        seconds=12, vod_id="55", offset=100,
    )
    plan = clipazos.ventana_vod(clip, 20, 35)
    assert plan is not None
    url, section, recorte = plan
    assert url == "https://www.twitch.tv/videos/55"
    assert section == (80.0, 47.0)
    assert recorte == (20.0, 32.0)
    assert clipazos.ventana_vod(clipazos.Candidate("Twitch", "A", "l", 1, "u", "k", seconds=30, vod_id="1", offset=10)) is None
    assert clipazos.ventana_vod(clipazos.Candidate("Twitch", "A", "l", 1, "u", "k", seconds=8)) is None

    calls = []

    def bajar(url, section, destination):
        calls.append((url, section))
        if "videos/55" in url:
            raise RuntimeError("caducado")
        destination.write_bytes(b"ok")

    monkeypatch.setattr(clipazos, "_bajar_medio", bajar)
    monkeypatch.setattr(clipazos, "CACHE_DIR", tmp_path)
    config = {"seleccion": {"historia": {"activa": True}}}
    path = clipazos.fetch_media(clip, config=config)
    assert path.is_file()
    assert calls[0][0].endswith("/videos/55") and calls[1][0] == "https://clips.twitch.tv/Abc"
    assert clip.nota_fuente.startswith("VOD no disponible")
    assert clip.clip_en_fuente is None

    bien = clipazos.Candidate(
        "Twitch", "Ibai", "otro", 10, "https://clips.twitch.tv/Bien", "twitch-Bien",
        seconds=8, vod_id="9", offset=40,
    )
    calls.clear()

    def bajar_ok(url, section, destination):
        calls.append(url)
        destination.write_bytes(b"ok")

    monkeypatch.setattr(clipazos, "_bajar_medio", bajar_ok)
    clipazos.fetch_media(bien, config=config)
    assert calls == ["https://www.twitch.tv/videos/9"]
    assert bien.nota_fuente == "ventana de VOD"
    assert bien.clip_en_fuente[0] == 20


def test_el_pico_se_busca_solo_dentro_del_clip_original():
    audio = np.zeros(int(8 * clipazos.SAMPLE_RATE), dtype=np.float32)
    # Un salto fuerte al principio de la ventana y otro dentro del clip (4-6 s).
    audio[int(0.2 * clipazos.SAMPLE_RATE):int(0.4 * clipazos.SAMPLE_RATE)] = 1
    audio[int(5.0 * clipazos.SAMPLE_RATE):int(5.2 * clipazos.SAMPLE_RATE)] = 0.2
    pico = clipazos.find_peak(audio, [], 4.0, 6.5)
    assert 4.0 <= pico <= 6.5


def test_kick_guarda_la_duracion_si_viene():
    candidate = clipazos._candidato_kick(
        {"id": "z", "url": "https://kick.com/davo/clips/z", "title": "t", "duration": 18, "channel": "davo"},
        "davo", 10, __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc),
    )
    assert candidate.seconds == 18 and candidate.vod_id is None


def test_ranking_juzga_a_todos_y_se_queda_el_mejor(monkeypatch):
    def build(candidate, *args, **kwargs):
        score = {"Ana": 0.4, "Beto": 0.97, "Carla": 0.2}[candidate.streamer]
        moment = clipazos.Moment(candidate, clipazos.BASE_DIR / "x.mp4", 0.5, 30.0, [])
        moment.segment = {"end": 30.5, "notes": []}
        moment.judgement = {
            "aceptado": score >= 0.9,
            "puntuacion": score,
            "motivos": [] if score >= 0.9 else ["historia corta"],
            "criterios": {},
        }
        moment.source_words = [
            CaptionCue("Vale.", 0.5, 1.0),
            CaptionCue("Sigue el desarrollo del momento.", 2.0, 4.0),
            CaptionCue("Y este es el payoff.", 20.0, 22.0),
        ]
        return moment

    monkeypatch.setattr(clipazos, "build_moment", build)
    pool = []
    for name, views in (("Ana", 5000), ("Ana", 4000), ("Beto", 10), ("Carla", 9000)):
        pool.append(clipazos.Candidate("Twitch", name, "t", views, "u", f"twitch-{name}-{views}", chat_rate=0))
    config = {
        "rights": {"unknown_permission_policy": "allow"},
        "seleccion": {"historia": {"activa": True, "candidatos_por_ejecucion": 3}},
    }
    orden = [item.streamer for item in clipazos._reparto_por_streamer(pool, 3)]
    assert orden == ["Carla", "Ana", "Beto"]
    chosen = clipazos.evaluar_por_juez(pool, config, "es")
    assert [moment.candidate.streamer for moment in chosen] == ["Beto"]
    assert chosen[0].judgement["puntuacion"] == 0.97
    arco = clipazos.explicar_arco(chosen[0])
    assert arco["gancho"]["texto"].startswith("Vale")
    assert "desarrollo" in arco and arco["payoff"]["texto"].startswith("Y este")
    assert arco["gancho"]["inicio"] < arco["payoff"]["inicio"]


def test_arco_no_cambia_la_nota(monkeypatch):
    words = palabras([
        ("Vale chat mirad esto con calma antes del grito.", 0.0),
        ("NOOO", 0.2),
        ("Y aquí está el remate completo del momento.", 0.2),
    ])
    peak = next(word.start for word in words if word.text.startswith("NOOO"))
    segment = ns.Segment(words[0].start - 0.1, words[-1].end + 0.2, peak, 0, len(words) - 1, complete_start=True, complete_end=True, sentences=3)
    verdict = clip_judge.judge(words, segment, length=12, config={"seleccion": {"segundos_min": 8, "segundos_max": 40}})
    assert "arco" in verdict.criteria
    pesos = sum(clip_judge.WEIGHTS.values())
    esperado = sum(clip_judge.WEIGHTS[name] * verdict.criteria[name]["puntos"] for name in clip_judge.WEIGHTS) / pesos
    assert abs(verdict.score - esperado) < 1e-9
    assert clip_judge.WEIGHTS["arco"] == 0


def test_un_clip_corto_pesa_la_mitad(monkeypatch):
    monkeypatch.setattr(clipazos, "attach_chat", lambda _candidates: None)
    corto = clipazos.Candidate("Kick", "Ana", "c", 1000, "u", "k-c", seconds=11, chat_rate=0)
    largo = clipazos.Candidate("Kick", "Ana", "l", 1000, "u", "k-l", seconds=40, chat_rate=0)
    assert clipazos.rank_clips([corto, largo])[0].key == "k-l"
