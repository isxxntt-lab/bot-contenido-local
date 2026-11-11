# Archivo del bot de streamers

Aquí está el código que solo servía para clips de Twitch, Kick y YouTube
(streamers, chat, radar, layouts de webcam, derechos de ese formato y sus pruebas).
Se movió con `git mv`: el historial de cada archivo sigue en git.

No está en el camino de importación. Pytest no entra en esta carpeta.

La copia ejecutable y completa del bot, con la config de producción y el WIP del
juez contextual, es la rama `archivo-streamers-20261111` en el commit
`6d6870e7d08729c55aa7d7f039a3f4fae416cf59`. Esta carpeta no sustituye a esa rama:
es el recorte que se apartó al pivotar.

`streamers/clipazos.streamers.json` es la config de aquel formato, incluidas las
secciones `twitch`, `kick`, `youtube`, `rights` y `seleccion.historia`.

`tests/test_clip_judge.py` se archivó con el segmentador: solo ejercitaba
`narrative_segments` y las transcripciones de ejemplo. El marco de puntuación
sigue en `clip_judge.py` y tiene pruebas nuevas que no dependen de ese módulo.
