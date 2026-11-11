# Formato: datos curiosos

Este paquete es solo el sitio del formato nuevo. No genera guiones, no llama a
Edge TTS y no descarga visuales. Eso lo hará otro agente a partir de un diseño
aparte. Los stubs levantan `NotImplementedError`.

## Flujo previsto

1. **Guion** (`guion.escribir_guion`): gancho, dato, explicación y cierre.
2. **Verificación** (`verificacion.verificar`): el dato se contrasta con dos fuentes.
   Sin dos fuentes, no se sigue.
3. **Voz** (`voz.sintetizar`): Edge TTS y eventos WordBoundary (texto, inicio, fin).
   Esas marcas son las `palabras` de `base.render.Pieza`.
4. **Visuales** (`visuales.buscar`): planos de Pexels, Pixabay o imagen generada,
   cada uno con licencia registrada (origen, autor, URL).
5. **Render** (`base.render.render_pieza`): vertical 1080x1920, pista de voz,
   subtítulos palabra a palabra y la calidad de `clipazos.json` (`render` y `subtitulos`).
6. **QA** (`qa.run_qa`) y **juez de guion** (el marco de `clip_judge.judge`).
7. **Salida**: YouTube con `youtube_upload` y carpeta TikTok con `tiktok_export`.
   `base.salida.registrar_exportacion` exporta si se le pasa el veredicto del juez.

`pipeline.ejecutar` describe ese orden y, de momento, no hace ninguno de los pasos.
