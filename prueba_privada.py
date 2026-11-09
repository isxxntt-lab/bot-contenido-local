"""Modo privado de prueba de Clipazos ES. No publica nada.

Entrada aparte de ``clipazos.py`` y de ``--dry-run``. Ese modo sigue filtrando
derechos y puede llegar a la comprobación de subida. Este módulo no importa
``youtube_upload``, ``publication_guard`` ni ``tiktok_export``, no reserva cupo
y no escribe en ``clipazos.json``.

Los derechos ``unknown`` se pueden transcribir y montar solo como ensayo.
Siguen en ``unknown``, con ``publication_eligible: false``. Lo denegado, los
términos bloqueados y el contenido de terceros no se procesan.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import clip_judge
import clipazos
import narrative_segments
import qa
import rights
from video_editor import _ffmpeg_exe, _filter_path

JUEZ_MODULO = "clip_judge.judge"
AVISO = "PRUEBA PRIVADA — NO PUBLICAR; derechos de origen sin verificar cuando corresponda"
UNKNOWN_MARK = "sin permiso conocido"
CREDIT = re.compile(r"@([^\s(]+)\s+\((Twitch|Kick|YouTube)\):\s*(https://[^\s\\\"']+)")
GOODBYE = {"despedida", "adios", "adiós", "chao", "bye", "nos vemos"}


def require_judge() -> None:
    """Fail closed: sin el juez real no se genera ningún ensayo."""
    if not callable(getattr(clip_judge, "judge", None)):
        raise RuntimeError("clip_judge.judge no está disponible; no se genera nada")


_CODE_FILES = (
    "prueba_privada.py",
    "clip_judge.py",
    "narrative_segments.py",
    "clipazos.py",
    "juez_contextual.py",
    "paquete_contextual.py",
)
_PRIVATE_NAME = (".env", "credencial", "token", "secret", "client_secret")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _git(args: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=clipazos.BASE_DIR, capture_output=True, check=False,
        )
    except OSError:
        return None


def _hide_private(path: str) -> bool:
    folded = path.casefold()
    return any(part in folded for part in _PRIVATE_NAME)


def code_provenance() -> dict:
    """Hash del código en disco y, si git responde, si ese código es el del commit.

    Si git no está o el directorio no es un repo, el estado queda ``desconocido``.
    No se declara limpio. No se listan rutas de secretos ni el árbol ignored entero.
    """
    digests = {name: _file_sha256(clipazos.BASE_DIR / name) for name in _CODE_FILES}
    report: dict = {
        "sha256": digests,
        "git": None,
        "git_estado": "desconocido",
        "motivo": "",
        "dirty": None,
        "tracked_modificados": [],
        "untracked": [],
        "ignored_codigo": [],
        "ignored_otros": None,
        "archivos_iguales_al_commit": {},
        "runner_en_commit": None,
    }
    head = _git(["rev-parse", "HEAD"])
    if head is None or head.returncode != 0 or not head.stdout.strip():
        detail = ""
        if head is not None:
            detail = (head.stderr or head.stdout).decode("utf-8", "replace").strip()
        report["motivo"] = detail or "git no devolvió un commit"
        return report
    report["git"] = head.stdout.decode().strip()
    status = _git(["status", "--porcelain=v1", "--ignored=matching", "--untracked-files=normal"])
    if status is None or status.returncode != 0:
        detail = ""
        if status is not None:
            detail = (status.stderr or status.stdout).decode("utf-8", "replace").strip()
        report["motivo"] = detail or "git status falló"
        return report
    tracked: list[str] = []
    untracked: list[str] = []
    ignored_code: list[str] = []
    ignored_others = 0
    private_omitidos = 0
    for raw in status.stdout.decode("utf-8", "replace").splitlines():
        if len(raw) < 4:
            continue
        code, path = raw[:2], raw[3:].strip().strip('"')
        if _hide_private(path):
            private_omitidos += 1
            continue
        if code == "!!":
            if Path(path).name in _CODE_FILES:
                ignored_code.append(path)
            else:
                ignored_others += 1
        elif code == "??":
            untracked.append(path)
        elif code.strip():
            tracked.append(path)
    report["tracked_modificados"] = tracked
    report["untracked"] = untracked
    report["ignored_codigo"] = ignored_code
    report["ignored_otros"] = ignored_others
    report["privados_omitidos"] = private_omitidos
    same: dict[str, bool] = {}
    for name in _CODE_FILES:
        exists = _git(["cat-file", "-e", f"HEAD:{name}"])
        diff = _git(["diff", "--quiet", "HEAD", "--", name])
        same[name] = (
            exists is not None and exists.returncode == 0
            and diff is not None and diff.returncode == 0
        )
    report["archivos_iguales_al_commit"] = same
    report["runner_en_commit"] = all(same.values())
    if tracked or untracked or ignored_code or private_omitidos or not report["runner_en_commit"]:
        report["git_estado"] = "sucio"
        report["dirty"] = True
    else:
        report["git_estado"] = "limpio"
        report["dirty"] = False
    return report


def judge_version(config: dict) -> dict:
    settings = clip_judge.settings_from(config)
    selection = narrative_segments.settings_from(config)
    version = {
        "modulo": JUEZ_MODULO,
        "pesos": dict(clip_judge.WEIGHTS),
        "puntuacion_min": settings["puntuacion_min"],
        "obligatorios": list(settings["obligatorios"]),
        "segundos_min": selection["segundos_min"],
        "segundos_max": selection["segundos_max"],
        "cierre_colgante": "rechazo invariante; no está en obligatorios y no cambia el peso",
    }
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=clipazos.BASE_DIR, capture_output=True, text=True, check=False,
        ).stdout.strip()
    except OSError:
        head = ""
    if head:
        version["git"] = head
    return version


def isolation_ok() -> bool:
    """Este módulo no tiene el uploader ni la guardia en su espacio de nombres."""
    forbidden = ("youtube_upload", "publication_guard", "tiktok_export")
    return not any(name in globals() for name in forbidden)


def load_config(path: Path) -> dict:
    """Lee una configuración. No la escribe."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _restricted(decision: rights.RightsDecision) -> list[str]:
    return [reason for reason in decision.reasons if UNKNOWN_MARK not in reason]


def private_decision(candidate, config: dict) -> dict:
    """Separa el bloqueo de publicación del permiso para ensayar en local.

    No cambia ``unknown_permission_policy`` ni el registro de streamers.
    """
    decision = rights.check_candidate(candidate, config)
    restricted = list(dict.fromkeys(
        _restricted(decision) or ([f"estado {decision.status}"] if decision.status == "denied" else [])
    ))
    ensayable = not restricted and (decision.allowed or decision.status == "unknown")
    return {
        "ensayable": ensayable,
        "rights_status": decision.status,
        "rights": decision.to_dict(),
        "restringido": restricted,
    }


def desktop_dir() -> Path:
    """Escritorio real (Known Folder), también si está en OneDrive."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            buffer = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buffer) == 0 and buffer.value:
                return Path(buffer.value)
        except OSError:
            pass
    home = Path.home()
    for candidate in (home / "Desktop", home / "Escritorio", home / "OneDrive" / "Desktop"):
        if candidate.is_dir():
            return candidate
    return home / "Desktop"


def _cache_file(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{clipazos._safe_name(key)}.mp4"


def _from_url(platform: str, streamer: str, url: str, title: str = "", game: str = "") -> clipazos.Candidate | None:
    url = url.rstrip(").,\\\"")
    if platform.casefold() == "twitch":
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        if "clips.twitch.tv" not in url or not slug:
            return None
        key = f"twitch-{slug}"
    elif platform.casefold() == "kick":
        match = re.search(r"(clip_[A-Za-z0-9]+)", url)
        if not match:
            return None
        key = f"kick-{match.group(1)}"
    else:
        return None
    return clipazos.Candidate(platform, streamer, title or "(caché)", 0, url, key, game or None)


def catalog(report_dir: Path, cache_dir: Path) -> list[clipazos.Candidate]:
    """Clips ya descargados cuya ficha está en un reporte. No descarga nada."""
    found: dict[str, clipazos.Candidate] = {}

    def remember(candidate: clipazos.Candidate | None) -> None:
        if candidate is None:
            return
        cache = _cache_file(cache_dir, candidate.key)
        if not cache.is_file() or cache.stat().st_size <= 0:
            return
        current = found.get(candidate.key)
        if current is None or (current.title == "(caché)" and candidate.title != "(caché)"):
            found[candidate.key] = candidate

    for path in sorted(report_dir.glob("reporte_*.json")):
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, ValueError):
            continue
        for short in data.get("shorts") or []:
            for source in short.get("fuentes") or []:
                if not isinstance(source, dict):
                    continue
                remember(_from_url(
                    str(source.get("platform") or ""),
                    str(source.get("streamer") or ""),
                    str(source.get("url") or ""),
                    str(source.get("title") or ""),
                    str(source.get("game") or ""),
                ))
            for streamer, platform, url in CREDIT.findall(str(short.get("descripcion") or "")):
                remember(_from_url(platform, streamer, url))
    return _ordered(list(found.values()))


def _ordered(candidates: list[clipazos.Candidate]) -> list[clipazos.Candidate]:
    priority = ["vegetta777", "ibai", "auronplay", "illojuan"]
    buckets: dict[str, list[clipazos.Candidate]] = {}
    for candidate in candidates:
        buckets.setdefault(candidate.streamer.casefold(), []).append(candidate)
    names = sorted(buckets, key=lambda name: (name not in priority, priority.index(name) if name in priority else 99, name))
    ordered: list[clipazos.Candidate] = []
    while any(buckets.values()):
        for name in names:
            if buckets[name]:
                ordered.append(buckets[name].pop(0))
    return ordered


def _goodbye(title: str) -> bool:
    folded = title.casefold().strip()
    return folded in GOODBYE or folded.startswith("despedida ")


def _blank_judge(config: dict, executed: bool = False) -> dict:
    return {
        "version": judge_version(config),
        "ejecutado": executed,
        "aceptado": None,
        "puntuacion": None,
        "motivos": [],
        "criterios": {},
    }


def _base_record(candidate, config: dict, gate: dict) -> dict:
    warning = ""
    if gate["rights_status"] != "allowed":
        warning = "Derechos de origen sin verificar. Prueba privada: no autoriza a publicar ni a subir a mano."
    return {
        "test_only": True,
        "publication_eligible": False,
        "ready_for_manual_upload": False,
        "estado": "test_rejected",
        "rights_status": gate["rights_status"],
        "rights": gate["rights"],
        "advertencia": warning,
        "rechazado_por": [],
        "juez": _blank_judge(config),
        "qa": None,
        "fuente": {
            "plataforma": candidate.platform,
            "streamer": candidate.streamer,
            "titulo": candidate.title,
            "juego": candidate.game or "",
            "url": candidate.url,
            "key": candidate.key,
        },
    }


def _arco(moment: clipazos.Moment) -> dict:
    cues = moment.cues or []
    peak = float(moment.peak or 0)
    hook = " ".join(cue.text for cue in cues if cue.start < 3).strip()
    context = " ".join(cue.text for cue in cues if 3 <= cue.start < peak).strip()
    payoff = " ".join(cue.text for cue in cues if cue.start >= peak).strip()
    return {"hook": hook, "contexto": context, "payoff": payoff}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _label(source: Path, destination: Path) -> None:
    font = _filter_path(Path(r"C:\Windows\Fonts\arialbd.ttf"))
    draw = (
        f"drawtext=fontfile='{font}':text='PRUEBA NO PUBLICAR':fontsize=32:"
        "fontcolor=white:box=1:boxcolor=black@0.65:x=24:y=16"
    )
    clipazos._run_ffmpeg([
        _ffmpeg_exe(), "-y", "-v", "error", "-i", str(source), "-vf", draw,
        "-c:a", "copy", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(destination),
    ])


def _cover(video: Path, second: float, destination: Path) -> None:
    clipazos._run_ffmpeg([
        _ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{max(0.0, second):.3f}",
        "-i", str(video), "-frames:v", "1", "-q:v", "2", str(destination),
    ])


def _write_md(path: Path, record: dict) -> None:
    judge = record.get("juez") or {}
    arc = record.get("arco") or {}
    source = record.get("fuente") or {}
    lines = [
        f"# {record.get('estado')} · @{source.get('streamer')}",
        "",
        AVISO,
        "",
        f"- URL: {source.get('url')}",
        f"- Tramo de origen: {record.get('tramo')}",
        f"- Layout: {record.get('layout')}",
        f"- Derechos: {record.get('rights_status')} · publication_eligible={record.get('publication_eligible')}",
        f"- Juez {judge.get('version', {}).get('modulo')} ({judge.get('version', {}).get('git', '')}): "
        f"ejecutado={judge.get('ejecutado')} puntuación={judge.get('puntuacion')} aceptado={judge.get('aceptado')}",
        f"- Motivos del juez: {'; '.join(judge.get('motivos') or []) or '—'}",
        f"- Abstenciones: {'; '.join(judge.get('abstenciones') or []) or '—'}",
        f"- Avisos no bloqueantes: {'; '.join(judge.get('avisos') or []) or '—'}",
        f"- Rechazado por: {', '.join(record.get('rechazado_por') or []) or '—'}",
        f"- QA: {record.get('qa')}",
        "",
        "## Arco (palabras del tramo elegido)",
        "",
        f"- Hook: {arc.get('hook') or '—'}",
        f"- Contexto: {arc.get('contexto') or '—'}",
        f"- Payoff: {arc.get('payoff') or '—'}",
        "",
        record.get("advertencia") or "",
        "",
    ]
    if record.get("layout") == "split":
        lines.append("Mejora aplicada: CaptionSplit con el borde del renglón en SPLIT_TOP (fuera de la webcam).")
    elif record.get("layout"):
        lines.append(f"CaptionSplit no entra en este clip: el layout real es {record.get('layout')}.")
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _caption(record: dict) -> str:
    source = record["fuente"]
    return (
        f"{AVISO}\n\n"
        f"Ensayo de @{source['streamer']} ({source['plataforma']}).\n"
        f"«{source['titulo']}»\n"
        f"{source['url']}\n\n"
        "No publicar. Los derechos de origen no están verificados.\n"
    )


def evaluate_candidate(candidate, config: dict, folder: Path, cache_dir: Path, contextual=None) -> dict:
    """Evalúa un clip de la caché. No descarga y no publica."""
    require_judge()
    folder.mkdir(parents=True, exist_ok=True)
    gate = private_decision(candidate, config)
    record = _base_record(candidate, config, gate)
    if not gate["ensayable"]:
        record["rechazado_por"] = ["derechos_restringidos"]
        record["detalle_restriccion"] = gate["restringido"]
        _finish(folder, record)
        return record
    if _goodbye(candidate.title):
        record["rechazado_por"] = ["despedida_rutinaria"]
        record["detalle"] = "El título es una despedida rutinaria; no se monta ni se exporta."
        _finish(folder, record)
        return record
    cache = _cache_file(cache_dir, candidate.key)
    if not cache.is_file() or cache.stat().st_size <= 0:
        record["rechazado_por"] = ["error_tecnico"]
        record["detalle"] = "No está en la caché local. Este modo no descarga para no pasar un muro o una cuenta."
        _finish(folder, record)
        return record
    try:
        moment = clipazos.build_moment(
            candidate,
            narrative_segments.settings_from(config)["segundos_min"],
            narrative_segments.settings_from(config)["segundos_max"],
            config.get("idioma", "es"),
            config,
            cache_dir=cache_dir,
        )
    except Exception as exc:  # noqa: BLE001 - un clip roto no publica nada; se apunta y se sigue
        record["rechazado_por"] = ["error_tecnico"]
        record["detalle"] = str(exc)
        _finish(folder, record)
        return record
    judgement = moment.judgement or {}
    record["juez"] = {
        "version": judge_version(config),
        "ejecutado": True,
        "aceptado": judgement.get("aceptado"),
        "puntuacion": judgement.get("puntuacion"),
        "motivos": judgement.get("motivos") or [],
        "avisos": judgement.get("avisos") or [],
        "abstenciones": judgement.get("abstenciones") or [],
        "decision": judgement.get("decision"),
        "criterios": judgement.get("criterios") or {},
    }
    record["arco"] = _arco(moment)
    record["tramo"] = {
        "inicio_origen": round(moment.start, 2),
        "duracion_final": round(moment.length, 2),
        "pico_short": round(moment.peak, 2),
        "segmento": moment.segment,
    }
    record["layout"] = moment.layout.kind if moment.layout else None
    if not judgement.get("aceptado"):
        record["rechazado_por"] = ["abstencion" if judgement.get("decision") == "abstencion" else "juez"]
        record["contextual"] = {"decision": "no_llamado", "motivo": "el juez textual no aceptó; no se rescata"}
        _finish(folder, record)
        return record
    if contextual is not None:
        try:
            contextual_verdict = contextual(moment, cache)
        except Exception as exc:  # noqa: BLE001 - un fallo del runtime no aprueba el clip
            contextual_verdict = {"decision": "abstain", "motivo": str(exc), "valido": False}
        record["contextual"] = contextual_verdict
        if contextual_verdict.get("decision") != "accept":
            record["rechazado_por"] = ["contextual"]
            _finish(folder, record)
            return record
    try:
        rendered = folder / "render.mp4"
        clipazos.render_short(
            [moment], [[("PRUEBA", True)], [("NO PUBLICAR", False)]], "TEST", rendered,
        )
        labeled = folder / "PRUEBA_NO_PUBLICAR.mp4"
        _label(rendered, labeled)
        ass = rendered.with_suffix(".ass")
        if ass.is_file():
            shutil.copyfile(ass, folder / "subtitulos.ass")
        result = qa.run_qa(labeled, config, ass if ass.is_file() else None)
    except Exception as exc:  # noqa: BLE001
        record["rechazado_por"] = ["error_tecnico"]
        record["detalle"] = str(exc)
        _finish(folder, record)
        return record
    record["qa"] = result.to_dict()
    if not result.passed:
        record["rechazado_por"] = ["qa"]
        _finish(folder, record)
        return record
    try:
        probe = qa.probe(labeled)
    except qa.QAToolError as exc:
        record["rechazado_por"] = ["error_tecnico"]
        record["detalle"] = str(exc)
        _finish(folder, record)
        return record
    second = min(float(moment.peak or 0), max(0.0, (probe.get("duracion") or 1) - 0.05))
    _cover(labeled, second, folder / "cover.jpg")
    (folder / "caption.txt").write_text(_caption(record), encoding="utf-8")
    record["estado"] = "test_passed"
    record["probe"] = probe
    record["portada"] = {"segundo": round(second, 3), "archivo": "cover.jpg"}
    record["video"] = {
        "archivo": "PRUEBA_NO_PUBLICAR.mp4",
        "bytes": labeled.stat().st_size,
        "sha256": _sha256(labeled),
    }
    record["publication_eligible"] = False
    record["test_only"] = True
    record["ready_for_manual_upload"] = False
    _finish(folder, record)
    return record


def plan_contextual(cuts):
    """La misma receta de tres fotogramas que usa el ensayo."""
    import paquete_contextual

    return paquete_contextual.frame_plan(cuts)


def materializar_receta(video: Path, cuts, folder: Path) -> list[dict] | None:
    """Escribe los tres fotogramas de la receta común. No rotula ni reencuadra."""
    import paquete_contextual

    plan = paquete_contextual.frame_plan(cuts)
    if len(plan) != paquete_contextual.FRAME_COUNT:
        return None
    written = []
    for item in plan:
        dest = folder / f"punto_{item['indice']}" / "frame.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [_ffmpeg_exe(), "-y", "-v", "error", "-ss", item["seek"], "-i", str(video),
             "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", str(dest)],
            capture_output=True,
        )
        if result.returncode != 0 or not dest.is_file() or dest.stat().st_size <= 0:
            return None
        stamped = dict(item)
        stamped["materializado"] = True
        stamped["archivo"] = str(dest)
        stamped["sha256"] = paquete_contextual.sha256_file(dest)
        stamped["bytes"] = dest.stat().st_size
        written.append(stamped)
    return written


def _leer_entrada(package: dict, schema: dict, modelo: str, digest: str):
    """Relee palabras, fotogramas y cuerpo. None si el disco ya no coincide."""
    import juez_contextual
    import paquete_contextual

    coverage = paquete_contextual.cobertura_entrada(package)
    frames = package.get("fotogramas") or []
    if coverage is None or any(not item.get("materializado") for item in frames):
        return None
    imagenes = []
    for item in frames:
        path = Path(item["archivo"])
        if not path.is_file():
            return None
        blob = path.read_bytes()
        if paquete_contextual.sha256_file(path) != hashlib.sha256(blob).hexdigest():
            return None
        if item.get("sha256") and item["sha256"] != hashlib.sha256(blob).hexdigest():
            return None
        imagenes.append(blob)
    prompt = juez_contextual.prompt_for(coverage)
    fuente = ((package.get("identidad") or {}).get("fuente") or {}).get("sha256")
    instantanea = paquete_contextual.instantanea_entrada(
        prompt=prompt,
        schema=schema,
        imagenes=imagenes,
        modelo=modelo,
        digest=digest,
        num_ctx=paquete_contextual.NUM_CTX,
        fuente_sha=fuente,
        mapa=package.get("mapa"),
        palabras_dentro=coverage["palabras"],
    )
    cuerpo = juez_contextual.build_request(modelo, prompt, [base64.b64encode(blob).decode("ascii") for blob in imagenes], paquete_contextual.NUM_CTX, 0)
    cuerpo_bytes = json.dumps(cuerpo, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    instantanea["cuerpo_sha256"] = hashlib.sha256(cuerpo_bytes).hexdigest()
    instantanea["cuerpo"] = cuerpo
    instantanea["prompt"] = prompt
    instantanea["imagenes"] = imagenes
    return instantanea


def _medir_paquete(package: dict, client, schema: dict) -> dict:
    """Preflight. La huella se relee después; no se compara una copia consigo misma."""
    import juez_contextual
    import paquete_contextual

    modelo = getattr(client, "modelo", None) or getattr(client, "model", None)
    digest = getattr(client, "digest", None)
    if not modelo or not digest:
        package = dict(package)
        package["preflight"] = {"aplicada": False, "motivo": "el cliente no identifica modelo y digest"}
        return package
    antes = _leer_entrada(package, schema, modelo, digest)
    if antes is None:
        package = dict(package)
        package["preflight"] = {"aplicada": False, "motivo": "faltan palabras de dentro o fotogramas hasheados"}
        return package
    images = [base64.b64encode(blob).decode("ascii") for blob in antes["imagenes"]]
    enviado = juez_contextual.cuerpo_preflight(antes["cuerpo"])
    wire = json.dumps(enviado).encode("utf-8")
    esperado = hashlib.sha256(wire).hexdigest()
    trace = client.preflight(antes["prompt"], images, antes["cuerpo"])
    if not isinstance(trace, dict):
        trace = {}
    despues = _leer_entrada(package, schema, modelo, digest)
    if despues is None or despues["huella"] != antes["huella"]:
        package = dict(package)
        package["preflight"] = {"aplicada": False, "motivo": "la entrada cambió durante la medición"}
        return package
    despues["cuerpo_bytes_sha256"] = esperado
    if trace.get("cuerpo_bytes_sha256") != esperado:
        package = dict(package)
        package["preflight"] = {"aplicada": False, "motivo": "el transporte no envió los bytes construidos"}
        return package
    return paquete_contextual.aplicar_preflight(package, trace, despues)


def capturar_evidencia(video: Path, folder: Path, config: dict) -> dict:
    """ASR de origen y tres fotogramas de una fuente ya en disco. No renderiza un short."""
    import paquete_contextual

    folder.mkdir(parents=True, exist_ok=True)
    probe = qa.probe(video)
    audio = clipazos.load_audio(video)
    words = clipazos.transcribe(audio, config.get("idioma", "es"))
    segment, length, _cues, _peak, cuts, _judgement = clipazos.select_segment(audio, words, config)
    moment = type("Captura", (), {})()
    moment.start = segment.start
    moment.cuts = list(cuts)
    moment.source_words = list(words)
    moment.segment = segment.to_dict()
    moment.length = length
    package = paquete_contextual.paquete_de_momento(moment, video, duracion_fuente=probe.get("duracion"))
    frames = materializar_receta(video, list(cuts), folder / "frames")
    if frames:
        package["fotogramas"] = frames
        package["receta"] = dict(package["receta"])
        package["receta"]["materializada"] = True
    package["origen_palabras"] = {
        "motor": "faster-whisper",
        "modelo": clipazos.WHISPER_MODEL,
        "dispositivo": "cpu",
        "compute_type": "int8",
        "reloj": "segundos de la fuente",
        "n": len(words),
    }
    package["selector"] = {
        "funcion": "clipazos.select_segment",
        "inicio": segment.start,
        "fin": segment.end,
        "duracion_corte": length,
        "exportado": False,
        "no_es_juicio_visual": True,
    }
    package["test_only"] = True
    package["publication_eligible"] = False
    package["ready_for_manual_upload"] = False
    package["ensayo_hecho"] = False
    package["rights_status"] = "unknown"
    return package


def _contextual_view(moment, video: Path, client, folder: Path, medicion=None, duracion_fuente: float | None = None) -> dict:
    """Arma el paquete. Si solo falta el presupuesto, mide la entrada y no juzga."""
    import juez_contextual
    import paquete_contextual

    package = paquete_contextual.paquete_de_momento(
        moment, video, duracion_fuente=duracion_fuente, medicion=medicion,
    )
    reason = paquete_contextual.motivo_de_bloqueo(package)
    cliente = "no_llamado"
    if reason.startswith("abstención: presupuesto") and getattr(client, "preflight", None):
        frames = materializar_receta(video, list(getattr(moment, "cuts", None) or []), folder)
        if not frames:
            reason = "abstención: no se materializaron los tres fotogramas de la receta"
        else:
            package["fotogramas"] = frames
            package["receta"] = dict(package["receta"])
            package["receta"]["materializada"] = True
            package = _medir_paquete(package, client, juez_contextual.OUTPUT_SCHEMA)
            cliente = "preflight"
            reason = paquete_contextual.motivo_de_bloqueo(package)
    if not reason and getattr(client, "complete", None):
        judged = _juzgar_una_vez(package, client, juez_contextual)
        judged["cliente"] = "juicio"
        return judged
    if not reason:
        reason = "presupuesto medido; la evaluación narrativa no está hecha"
    return {
        "decision": "abstain",
        "motivo": reason,
        "valido": False,
        "evidencias": [],
        "cliente": cliente,
        "paquete": package,
    }


def _juzgar_una_vez(package: dict, client, juez_contextual) -> dict:
    """Una llamada si la relectura sigue igual que la medición. No reintenta."""
    import paquete_contextual

    modelo = getattr(client, "modelo", None) or getattr(client, "model", None)
    digest = getattr(client, "digest", None)
    releida = _leer_entrada(package, juez_contextual.OUTPUT_SCHEMA, modelo or "", digest or "")
    congelado = (package.get("congelado") or {}).get("huella")
    if releida is None or not congelado or releida["huella"] != congelado:
        return {
            "decision": "abstain",
            "motivo": "la entrada ya no es la que se midió",
            "valido": False,
            "evidencias": [],
            "paquete": package,
            "llamadas_semanticas": 0,
            "cliente": "no_llamado",
        }
    images = [base64.b64encode(blob).decode("ascii") for blob in releida["imagenes"]]
    try:
        trace = client.complete(releida["prompt"], images)
    except Exception as exc:  # noqa: BLE001
        return {
            "decision": "abstain",
            "motivo": f"runtime contextual: {exc}",
            "valido": False,
            "evidencias": [],
            "paquete": package,
            "llamadas_semanticas": 1,
        }
    if not isinstance(trace, dict):
        trace = {"raw": str(trace), "error": None}
    coverage = paquete_contextual.cobertura_entrada(package)
    corr = paquete_contextual.correlacionar_observacion(trace) if isinstance(trace, dict) else {"valida": False, "motivo": "sin traza"}
    if trace.get("error") or not trace.get("raw") or not corr.get("valida"):
        verdict = {
            "decision": "abstain",
            "motivo": (trace.get("error") if isinstance(trace, dict) else None) or (None if corr.get("valida") else corr.get("motivo")) or "respuesta vacía",
            "valido": False,
            "evidencias": [],
        }
    else:
        verdict = juez_contextual.validate_decision(trace["raw"], coverage)
    verdict["raw"] = trace.get("raw") or ""
    verdict["traza"] = {
        key: trace.get(key)
        for key in ("done_reason", "prompt_eval_count", "eval_count", "error", "num_ctx", "num_predict")
    }
    verdict["paquete"] = package
    verdict["llamadas_semanticas"] = 1
    dentro = len(coverage["palabras"]) if coverage else 0
    limites = package.get("limites_cobertura") or {}
    verdict["cobertura_vista_por_el_modelo"] = {
        "dentro": dentro,
        "pre": limites.get("pre"),
        "post": limites.get("post"),
        "fuera_no_enviado": True,
        "contexto_completo": False,
        "tres_fotogramas_prueban_accion": False,
    }
    return verdict


def seguir_si_textual_acepto(judgement: dict, continuar) -> dict:
    """Una abstención o un rechazo textual no llama al modelo."""
    if not judgement.get("aceptado"):
        kind = "abstencion" if judgement.get("decision") == "abstencion" else "rechazo"
        return {
            "decision": "no_llamado",
            "cliente": "no_llamado",
            "motivo": f"el juez textual devolvió {kind}; no se rescata",
            "textual": {
                "decision": judgement.get("decision"),
                "aceptado": False,
                "motivos": judgement.get("motivos") or [],
                "abstenciones": judgement.get("abstenciones") or [],
            },
            "llamadas_preflight": 0,
            "llamadas_semanticas": 0,
        }
    return continuar()


def _finish(folder: Path, record: dict) -> None:
    (folder / "manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_md(folder / "evaluacion.md", record)


def _fresh(parent: Path, name: str) -> Path:
    destination = parent / name
    index = 2
    while destination.exists():
        destination = parent / f"{name}_{index}"
        index += 1
    destination.mkdir(parents=True)
    return destination


def run(
    config: dict,
    candidates: list,
    salida: Path,
    escritorio: Path | None,
    *,
    max_evaluar: int = 16,
    max_exportar: int = 5,
    cache_dir: Path | None = None,
    contextual=None,
) -> dict:
    """Evalúa como mucho ``max_evaluar`` cachés y exporta como mucho ``max_exportar`` aprobados."""
    require_judge()
    if not isolation_ok():
        raise RuntimeError("el modo privado tiene un uploader importado; no se genera nada")
    cache_dir = cache_dir or clipazos.CACHE_DIR
    salida.mkdir(parents=True, exist_ok=True)
    (salida / "README.txt").write_text(AVISO + "\n", encoding="utf-8")
    summary = {
        "test_only": True,
        "publication_eligible": False,
        "evaluados": 0,
        "exportados": 0,
        "rechazados": {
            "derechos_restringidos": 0, "despedida_rutinaria": 0, "juez": 0,
            "abstencion": 0, "qa": 0, "error_tecnico": 0,
        },
        "juez": judge_version(config),
        "procedencia": code_provenance(),
        "clips": [],
    }
    run_desktop = None
    judged = 0
    for candidate in candidates:
        if judged >= max_evaluar or summary["exportados"] >= max_exportar:
            break
        folder = _fresh(salida / "candidatos", clipazos._safe_name(candidate.key))
        record = evaluate_candidate(candidate, config, folder, cache_dir, contextual)
        summary["evaluados"] += 1
        if (record.get("juez") or {}).get("ejecutado"):
            judged += 1
        if record.get("publication_eligible") is not False or record.get("test_only") is not True:
            record["estado"] = "test_rejected"
            record["rechazado_por"] = list(record.get("rechazado_por") or []) + ["aislamiento"]
            record["publication_eligible"] = False
            record["ready_for_manual_upload"] = False
            _finish(folder, record)
        kind = (record.get("rechazado_por") or ["otro"])[0]
        if record["estado"] != "test_passed":
            summary["rechazados"][kind] = summary["rechazados"].get(kind, 0) + 1
        summary["clips"].append({
            "key": candidate.key,
            "streamer": candidate.streamer,
            "estado": record["estado"],
            "rights_status": record["rights_status"],
            "puntuacion": (record.get("juez") or {}).get("puntuacion"),
            "rechazado_por": record.get("rechazado_por"),
            "carpeta": str(folder),
        })
        print(
            f"{record['estado']} @{candidate.streamer} {candidate.key} "
            f"derechos={record['rights_status']} juez={(record.get('juez') or {}).get('puntuacion')}",
            flush=True,
        )
        if record["estado"] == "test_passed" and escritorio is not None:
            if run_desktop is None:
                run_desktop = _fresh(escritorio, salida.name)
                (run_desktop / "README.txt").write_text(AVISO + "\n", encoding="utf-8")
            copied = _fresh(run_desktop, folder.name)
            for item in folder.iterdir():
                target = copied / item.name
                if item.is_file():
                    shutil.copy2(item, target)
            summary["exportados"] += 1
            summary["clips"][-1]["escritorio"] = str(copied)
    summary["salida"] = str(salida)
    summary["escritorio"] = str(run_desktop) if run_desktop else None
    (salida / "resumen.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    require_judge()
    if not isolation_ok():
        print("Modo privado cancelado: hay un uploader importado en el módulo.", file=sys.stderr)
        return 3
    parser = argparse.ArgumentParser(description="Ensayo privado de Clipazos. No publica.")
    parser.add_argument("--config", type=Path, default=clipazos.CONFIG_PATH)
    parser.add_argument("--cache", type=Path, default=clipazos.CACHE_DIR)
    parser.add_argument("--reportes", type=Path, default=clipazos.CLIPAZOS_DIR)
    parser.add_argument("--salida", type=Path, default=None)
    parser.add_argument("--escritorio", type=Path, default=None)
    parser.add_argument("--max-evaluar", type=int, default=16)
    parser.add_argument("--max-exportar", type=int, default=5)
    parser.add_argument(
        "--juez-contextual",
        default="",
        help=(
            "URL loopback del modelo visual en vivo (/api/chat). Vacío: apagado. "
            "No lee un JSON de juicio ya calculado."
        ),
    )
    args = parser.parse_args(argv)
    config = load_config(args.config)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    salida = args.salida or (clipazos.CLIPAZOS_DIR / f"pruebas_{stamp}")
    if args.escritorio is None:
        escritorio = desktop_dir() / "Clipazos TikTok" / "PRUEBAS_NO_PUBLICAR"
    else:
        escritorio = args.escritorio
    candidates = catalog(args.reportes, args.cache)
    print(f"Caché con ficha: {len(candidates)}. Salida: {salida}", flush=True)
    contextual = None
    if args.juez_contextual:
        import juez_contextual

        client = juez_contextual.OllamaVision(args.juez_contextual, "qwen3-vl:4b-instruct")

        def contextual(moment, video, _client=client):
            return _contextual_view(moment, video, _client, salida / "_frames")

    run(
        config, candidates, salida, escritorio,
        max_evaluar=args.max_evaluar, max_exportar=args.max_exportar, cache_dir=args.cache,
        contextual=contextual,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
