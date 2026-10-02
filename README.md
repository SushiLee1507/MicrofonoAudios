# Soundboard Mic

Tablero de sonidos para Windows que reproduce tus audios por un micrófono virtual, mezclados con tu voz, y al mismo tiempo por tus audífonos para que tú también los escuches. Pensado para Discord y juegos.

## Qué hace

- Reproduce `.mp3`, `.wav`, `.ogg` y `.flac` por el micrófono virtual y por tus audífonos a la vez.
- Mezcla tu voz con los audios; una casilla (o un atajo) la quita cuando quieras.
- Volumen separado para los demás, para ti y para tu voz.
- Atajos de teclado globales por audio, para «Detener todo» y para activar/quitar la voz.
- Control desde Stream Deck mediante enlaces locales (acción «Website») o atajos (acción «Hotkey»).
- Al cerrar la ventana sigue funcionando en segundo plano, como icono junto al reloj.
- Guarda sola tu lista de audios, atajos y volúmenes.
- Menú «Requisitos» que comprueba qué tienes instalado y qué falta, e instala los paquetes de Python con un botón.

## Requisitos

- Windows 10 u 11.
- Python 3.9 o superior (con la opción «tcl/tk» del instalador).
- [VB-CABLE](https://vb-audio.com/Cable/), el controlador de micrófono virtual. Es gratuito y se instala aparte, como administrador.
- Paquetes de Python: `numpy`, `sounddevice`, `miniaudio`, `soundfile`, `keyboard`, `pystray`, `pillow`. La app los instala desde «Requisitos».

## Instalación

1. Descarga o clona este repositorio.
2. Abre `Iniciar.bat`. Si no tienes Python, te avisa y abre la página de descarga.
3. En la ventana «Requisitos», pulsa «Instalar lo que falta».
4. Instala VB-CABLE con el botón «Descargar VB-CABLE» y reinicia el PC.

## Uso

1. En la app, deja «Micrófono virtual» en **CABLE Input** y elige tu micrófono y tus audífonos.
2. En Discord o en el juego, elige **CABLE Output** como micrófono.
3. En Discord, apaga la supresión de ruido y la cancelación de eco, y pon la sensibilidad de entrada en manual y baja. Si no, Discord recorta los audios.
4. Agrega tus audios con «Agregar audios» y, si quieres, asígnales un atajo.

### Stream Deck

Pulsa «Stream Deck» en la app, copia el enlace del audio y pégalo en una acción «System → Website» con la casilla «GET request in background» marcada. Los enlaces solo responden desde el propio PC y llevan una clave.

## Problemas frecuentes

| Síntoma | Qué revisar |
| --- | --- |
| Error al abrir un dispositivo | Cambia «Sistema de audio» de WASAPI a MME. |
| Los demás no oyen nada | «Micrófono virtual» debe ser CABLE Input en la app y CABLE Output en Discord. |
| Los audios se cortan en Discord | Supresión de ruido, cancelación de eco y sensibilidad automática apagadas. |
| Los atajos no responden en el juego | Si el juego corre como administrador, abre la app también como administrador. |
| No aparece el icono junto al reloj | Faltan `pystray` o `pillow`; instálalos desde «Requisitos». |

## Archivos

- `soundboard.py`: la aplicación completa.
- `Iniciar.bat`: lanzador que comprueba Python y abre la app sin consola.
- `config.json`: se crea solo al usar la app. Guarda tus rutas, atajos y la clave de los enlaces; no lo subas al repositorio.

- `requirements.txt`: lista de paquetes, por si prefieres instalarlos a mano con `py -m pip install -r requirements.txt`.

## Licencia

El código es público y gratuito bajo la licencia [PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0): puedes usarlo, modificarlo y compartirlo sin costo, pero no venderlo ni usarlo con fines comerciales. Al compartirlo debes mantener el aviso de autoría y la licencia. Consulta el archivo `LICENSE`.

## Aviso

VB-CABLE es un producto de VB-Audio y no se incluye en este repositorio; descárgalo de su página oficial.
