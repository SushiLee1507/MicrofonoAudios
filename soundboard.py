#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Soundboard Mic
==============
Tablero de sonidos para Windows: reproduce audios (mp3, wav, ogg, flac) por un
microfono virtual (VB-CABLE) mezclados con tu voz, y al mismo tiempo por tus
audifonos para que tu tambien los escuches.

Abre "Requisitos" dentro de la app para ver que falta instalar.
"""

import collections
import importlib
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_NAME = "Soundboard Mic"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "error.log")
VBCABLE_URL = "https://vb-audio.com/Cable/"
SR = 48000            # frecuencia de trabajo (la que usa Discord)
MIC_TARGET_S = 0.05   # colchon de voz (se agranda solo si hay cortes)
MIC_MAX_TARGET_S = 0.20
IS_WINDOWS = sys.platform.startswith("win")

# (modulo, nombre en pip, para que sirve)
PACKAGES = [
    ("numpy", "numpy", "Mezcla del audio"),
    ("sounddevice", "sounddevice", "Enviar audio al micrófono virtual y a tus audífonos"),
    ("miniaudio", "miniaudio", "Leer los .mp3 completos (sin que se corten)"),
    ("soundfile", "soundfile", "Leer archivos .wav, .ogg y .flac"),
    ("keyboard", "keyboard", "Atajos de teclado globales (funcionan dentro del juego)"),
    ("pystray", "pystray", "Icono junto al reloj para seguir en segundo plano"),
    ("PIL", "pillow", "Dibujar el icono de segundo plano"),
]

np = sd = sf = kb = pystray = pil = miniaudio = None
MODULE_ERRORS = {}

# ---- colores -------------------------------------------------------------
BG = "#16171d"
PANEL = "#20222b"
PANEL2 = "#2a2d39"
FG = "#e8e9ee"
MUTED = "#9a9db0"
ACCENT = "#6c8cff"
OK = "#4cd08a"
BAD = "#ff6b6b"
WARN = "#ffc857"


def load_modules():
    """Importa las dependencias opcionales. Devuelve los paquetes que faltan."""
    global np, sd, sf, kb, pystray, pil, miniaudio
    importlib.invalidate_caches()
    MODULE_ERRORS.clear()
    loaded = {}
    missing = []
    for mod, pip_name, _ in PACKAGES:
        try:
            loaded[mod] = importlib.import_module(mod)
        except Exception as exc:  # ImportError u OSError (falta una DLL)
            loaded[mod] = None
            MODULE_ERRORS[mod] = str(exc)
            missing.append(pip_name)
    np = loaded["numpy"]
    sd = loaded["sounddevice"]
    sf = loaded["soundfile"]
    kb = loaded["keyboard"]
    pystray = loaded["pystray"]
    pil = loaded["PIL"]
    miniaudio = loaded["miniaudio"]
    return missing


# =========================================================================
#  Configuracion
# =========================================================================
DEFAULT_CONFIG = {
    "sounds": [],            # [{"name":..., "path":..., "hotkey":...}]
    "vol_others": 100,       # volumen de los audios para los demas (%)
    "vol_me": 60,            # volumen de los audios para mi (%)
    "vol_mic": 100,          # volumen de mi voz (%)
    "mic_enabled": True,     # enviar mi voz al microfono virtual
    "mic_device": "",
    "cable_device": "",
    "monitor_device": "",
    "hostapi": "Windows WASAPI",
    "hk_stop": "",
    "hk_mic": "",
    "api_port": 47800,       # control local para Stream Deck
    "api_token": "",
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg.update(data)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    if not isinstance(cfg.get("sounds"), list):
        cfg["sounds"] = []
    if not cfg.get("api_token"):
        cfg["api_token"] = secrets.token_hex(6)
    used = set()
    for snd in cfg["sounds"]:           # identificador fijo para los enlaces
        if not snd.get("id") or snd["id"] in used:
            snd["id"] = secrets.token_hex(3)
        used.add(snd["id"])
    return cfg


def save_config(cfg):
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass


# =========================================================================
#  Dispositivos
# =========================================================================
def rescan_devices():
    """Obliga a PortAudio a releer la lista de dispositivos de Windows."""
    if sd is None:
        return
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass


def hostapi_names():
    if sd is None:
        return []
    try:
        return [a["name"] for a in sd.query_hostapis()]
    except Exception:
        return []


def list_devices(hostapi_name):
    """Devuelve (entradas, salidas, entrada_por_defecto, salida_por_defecto)."""
    if sd is None:
        return [], [], None, None
    try:
        apis = sd.query_hostapis()
        devices = sd.query_devices()
    except Exception:
        return [], [], None, None
    api_idx = next((i for i, a in enumerate(apis) if a["name"] == hostapi_name), None)
    if api_idx is None:
        api_idx = next((i for i, a in enumerate(apis) if a["devices"]), 0)
    if not apis:
        return [], [], None, None
    api = apis[api_idx]
    ins, outs = [], []
    for i, d in enumerate(devices):
        if d["hostapi"] != api_idx:
            continue
        base = {"index": i, "name": d["name"], "api": api["name"]}
        if d["max_input_channels"] > 0:
            ins.append(dict(base, ch=d["max_input_channels"]))
        if d["max_output_channels"] > 0:
            outs.append(dict(base, ch=d["max_output_channels"]))
    def_in = next((d["name"] for d in ins if d["index"] == api.get("default_input_device")), None)
    def_out = next((d["name"] for d in outs if d["index"] == api.get("default_output_device")), None)
    return ins, outs, def_in, def_out


def is_virtual(name):
    n = name.lower()
    return "cable" in n and ("vb-audio" in n or "input" in n or "output" in n)


def find_cable_everywhere():
    """Busca VB-CABLE en todas las APIs de audio. (tiene_entrada, tiene_salida)"""
    if sd is None:
        return False, False
    try:
        devices = sd.query_devices()
    except Exception:
        return False, False
    play = any("cable input" in d["name"].lower() and d["max_output_channels"] > 0 for d in devices)
    rec = any("cable output" in d["name"].lower() and d["max_input_channels"] > 0 for d in devices)
    return play, rec


# =========================================================================
#  Motor de audio
# =========================================================================
class MicFifo:
    """Colchon que lleva la voz del microfono real al microfono virtual.

    El microfono y el cable virtual son dos dispositivos con relojes distintos y
    entregan el audio en bloques que no coinciden. Para que la voz no se corte:
      * se espera a tener un colchon (target) antes de empezar a enviar;
      * si aun asi se vacia, el colchon se agranda solo;
      * la diferencia de reloj se compensa estirando o encogiendo cada bloque
        una o dos muestras (inaudible), en lugar de tirar trozos de voz.
    """

    def __init__(self, target=None, cap=None):
        self.cap = int(cap or SR)
        self.buf = np.zeros(self.cap, dtype=np.float32)
        self.r = 0
        self.fill = 0
        self.target = int(target or SR * MIC_TARGET_S)
        self.max_target = max(self.target, int(SR * MIC_MAX_TARGET_S))
        self.primed = False
        self.avg = float(self.target)
        self.underruns = 0
        self.lock = threading.Lock()

    def clear(self):
        with self.lock:
            self.r = 0
            self.fill = 0
            self.primed = False

    def write(self, mono):
        n = len(mono)
        if n == 0:
            return
        with self.lock:
            if n > self.cap:
                mono = mono[-self.cap:]
                n = self.cap
            over = self.fill + n - self.cap
            if over > 0:                       # lleno: se pierde lo mas viejo
                self.r = (self.r + over) % self.cap
                self.fill -= over
            w = (self.r + self.fill) % self.cap
            first = min(n, self.cap - w)
            self.buf[w:w + first] = mono[:first]
            if first < n:
                self.buf[:n - first] = mono[first:]
            self.fill += n

    def _take(self, n):
        first = min(n, self.cap - self.r)
        out = np.empty(n, dtype=np.float32)
        out[:first] = self.buf[self.r:self.r + first]
        if first < n:
            out[first:] = self.buf[:n - first]
        self.r = (self.r + n) % self.cap
        self.fill -= n
        return out

    def read(self, n):
        with self.lock:
            if not self.primed:
                if self.fill < self.target + n:
                    return np.zeros(n, dtype=np.float32)
                extra = self.fill - (self.target + n)   # arrancar justo en el objetivo
                if extra > 0:
                    self.r = (self.r + extra) % self.cap
                    self.fill -= extra
                self.primed = True
                self.avg = float(self.target)
            # correccion suave de la diferencia de reloj (0,2 % como mucho)
            want = n
            k = max(1, n // 500)
            margin = self.target // 3
            if self.avg > self.target + margin:
                want = n + k
            elif self.avg < self.target - margin:
                want = n - k
            if self.fill < want:
                # se vacio: enviar lo que haya, volver a llenar y agrandar el colchon
                got = min(self.fill, n)
                out = np.zeros(n, dtype=np.float32)
                if got:
                    out[:got] = self._take(got)
                self.primed = False
                self.underruns += 1
                self.target = min(self.max_target, self.target + SR // 100)
                return out
            data = self._take(want)
            self.avg = 0.95 * self.avg + 0.05 * self.fill
        if want != n:
            data = np.interp(np.linspace(0, want - 1, n), np.arange(want), data).astype(np.float32)
        return data


class Voice:
    __slots__ = ("data", "pos")

    def __init__(self, data):
        self.data = data
        self.pos = 0


def decode_file(path):
    """Lee un archivo de audio y lo deja en estereo float32 a 48 kHz."""
    # 1) miniaudio: decodifica el archivo entero cuadro a cuadro. El otro lector
    #    (soundfile) se fia de la duracion que calcula al abrir y corta antes de
    #    tiempo los .mp3 de bitrate variable sin cabecera de duracion.
    if miniaudio is not None:
        try:
            with open(path, "rb") as fh:      # leer nosotros: rutas con tildes o eñes
                raw = fh.read()
            dec = miniaudio.decode(raw, output_format=miniaudio.SampleFormat.FLOAT32,
                                   nchannels=2, sample_rate=SR)
            data = np.frombuffer(dec.samples, dtype=np.float32).reshape(-1, 2)
            if data.shape[0] > 0:
                return np.array(data, dtype=np.float32, order="C")
        except Exception:
            if sf is None:
                raise
    if sf is None:
        raise RuntimeError("Falta un paquete para leer audio. Abre Requisitos.")
    # 2) soundfile como respaldo
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    if data.shape[0] == 0:
        raise ValueError("El archivo no contiene audio")
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = data[:, :2]
    if sr != SR:
        n_new = max(1, int(round(data.shape[0] * SR / sr)))
        x_old = np.arange(data.shape[0], dtype=np.float64)
        x_new = np.linspace(0, data.shape[0] - 1, n_new)
        data = np.stack([np.interp(x_new, x_old, data[:, c]) for c in range(2)], axis=1)
    return np.ascontiguousarray(data, dtype=np.float32)


class AudioEngine:
    def __init__(self):
        self.lock = threading.Lock()
        self.voices_cable = []
        self.voices_mon = []
        self.fifo = None
        self.cable_stream = None
        self.mon_stream = None
        self.mic_stream = None
        self.vol_others = 1.0
        self.vol_me = 0.6
        self.vol_mic = 1.0
        self.mic_enabled = True
        self.mic_level = 0.0

    # ---- flujos --------------------------------------------------------
    def _open(self, kind, dev, callback, latency=0.03):
        cls = sd.InputStream if kind == "in" else sd.OutputStream
        channels = max(1, min(2, dev["ch"]))
        attempts = []
        if "WASAPI" in dev["api"]:
            try:
                # Deja que Windows convierta la frecuencia si el dispositivo no usa 48 kHz
                attempts.append(sd.WasapiSettings(auto_convert=True))
            except Exception:
                pass
        attempts.append(None)
        last = None
        for extra in attempts:
            try:
                stream = cls(device=dev["index"], samplerate=SR, channels=channels,
                             dtype="float32", latency=latency, callback=callback,
                             extra_settings=extra)
                stream.start()
                return stream
            except Exception as exc:
                last = exc
        raise last

    def stop_streams(self):
        for attr in ("mic_stream", "cable_stream", "mon_stream"):
            stream = getattr(self, attr)
            setattr(self, attr, None)
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
        with self.lock:
            self.voices_cable.clear()
            self.voices_mon.clear()
        self.mic_level = 0.0

    def start(self, mic_dev, cable_dev, mon_dev):
        """Abre los tres flujos. Devuelve una lista de errores (texto)."""
        self.stop_streams()
        errors = []
        if sd is None or np is None:
            return ["Faltan paquetes de audio. Abre Requisitos."]
        self.fifo = MicFifo()
        if cable_dev:
            try:
                self.cable_stream = self._open("out", cable_dev, self._cable_cb)
            except Exception as exc:
                errors.append("Micrófono virtual (%s): %s" % (cable_dev["name"], exc))
        if mon_dev:
            try:
                self.mon_stream = self._open("out", mon_dev, self._mon_cb, latency=0.08)
            except Exception as exc:
                errors.append("Mis audífonos (%s): %s" % (mon_dev["name"], exc))
        if mic_dev and self.cable_stream is not None:
            try:
                self.mic_stream = self._open("in", mic_dev, self._mic_cb)
            except Exception as exc:
                errors.append("Mi micrófono (%s): %s" % (mic_dev["name"], exc))
        return errors

    # ---- callbacks (hilo de audio) --------------------------------------
    def _mix(self, voices, frames):
        buf = np.zeros((frames, 2), dtype=np.float32)
        with self.lock:
            alive = []
            for v in voices:
                n = min(frames, v.data.shape[0] - v.pos)
                if n > 0:
                    buf[:n] += v.data[v.pos:v.pos + n]
                    v.pos += n
                if v.pos < v.data.shape[0]:
                    alive.append(v)
            voices[:] = alive
        return buf

    @staticmethod
    def _write(outdata, buf):
        np.clip(buf, -1.0, 1.0, out=buf)
        if outdata.shape[1] == 1:
            outdata[:, 0] = buf.mean(axis=1)
        else:
            outdata[:, :2] = buf
            if outdata.shape[1] > 2:
                outdata[:, 2:] = 0

    def _cable_cb(self, outdata, frames, time_info, status):
        buf = self._mix(self.voices_cable, frames)
        buf *= self.vol_others
        fifo = self.fifo
        if fifo is not None:
            if self.mic_enabled and self.mic_stream is not None:
                mic = fifo.read(frames)
                mic *= self.vol_mic
                buf += mic[:, None]
            elif fifo.fill:
                fifo.clear()
        self._write(outdata, buf)

    def _mon_cb(self, outdata, frames, time_info, status):
        buf = self._mix(self.voices_mon, frames)
        buf *= self.vol_me
        self._write(outdata, buf)

    def _mic_cb(self, indata, frames, time_info, status):
        mono = indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0].copy()
        mono = mono.astype(np.float32, copy=False)
        peak = float(np.abs(mono).max()) if len(mono) else 0.0
        self.mic_level = max(peak, self.mic_level * 0.85)
        if self.mic_enabled and self.fifo is not None:
            self.fifo.write(mono)

    # ---- control --------------------------------------------------------
    def play(self, data):
        with self.lock:
            if self.cable_stream is not None:
                self.voices_cable.append(Voice(data))
            if self.mon_stream is not None:
                self.voices_mon.append(Voice(data))
        return self.cable_stream is not None or self.mon_stream is not None

    def stop_all(self):
        with self.lock:
            self.voices_cable.clear()
            self.voices_mon.clear()

    def playing(self):
        return max(len(self.voices_cable), len(self.voices_mon))


# =========================================================================
#  Comprobacion de requisitos
# =========================================================================
def check_requirements(cfg):
    """Lista de dicts: title, ok (True/False/None), detail, kind."""
    items = []
    v = sys.version_info
    items.append({
        "title": "Python 3.9 o superior",
        "ok": v >= (3, 9),
        "detail": "Tienes Python %d.%d.%d" % (v[0], v[1], v[2]) if v >= (3, 9)
        else "Tienes Python %d.%d. Instala una versión más nueva desde python.org" % (v[0], v[1]),
        "kind": "python",
    })
    mods = {"numpy": np, "sounddevice": sd, "soundfile": sf, "keyboard": kb,
            "pystray": pystray, "PIL": pil, "miniaudio": miniaudio}
    for mod, pip_name, purpose in PACKAGES:
        ok = mods[mod] is not None
        detail = purpose if ok else "%s. Falta instalarlo (botón «Instalar lo que falta»)." % purpose
        err = MODULE_ERRORS.get(mod, "")
        if not ok and err and "No module named" not in err:
            detail += "  Error: " + err[:120]
        items.append({"title": "Paquete «%s»" % pip_name, "ok": ok, "detail": detail, "kind": "pip"})

    if miniaudio is not None:
        items.append({"title": "Soporte para .mp3", "ok": True,
                      "detail": "Listo para reproducir .mp3 completos", "kind": "pip"})
    else:
        items.append({"title": "Soporte para .mp3", "ok": False,
                      "detail": "Sin «miniaudio» algunos .mp3 se cortan antes de terminar. "
                                "Usa «Instalar lo que falta».", "kind": "pip"})

    if sd is not None:
        play, rec = find_cable_everywhere()
        if play and rec:
            detail = "VB-CABLE instalado: «CABLE Input» y «CABLE Output» detectados."
        elif play or rec:
            detail = ("VB-CABLE está instalado pero una de sus dos partes está desactivada. "
                      "Actívala en Configuración de sonido de Windows.")
        else:
            detail = ("No está instalado. Es gratis y no requiere comprar licencia; "
                      "solo pide permiso de administrador para instalar el controlador.")
        items.append({"title": "Micrófono virtual (VB-CABLE)", "ok": play and rec,
                      "detail": detail, "kind": "cable"})

        ins, outs, _, _ = list_devices(cfg.get("hostapi", ""))
        real_ins = [d for d in ins if not is_virtual(d["name"])]
        real_outs = [d for d in outs if not is_virtual(d["name"])]
        items.append({
            "title": "Tu micrófono real",
            "ok": bool(real_ins),
            "detail": "%d micrófono(s) detectado(s)." % len(real_ins) if real_ins
            else "No se detecta ningún micrófono. Conéctalo o actívalo en Configuración de sonido.",
            "kind": "sound",
        })
        items.append({
            "title": "Tus audífonos o parlantes",
            "ok": bool(real_outs),
            "detail": "%d salida(s) de audio detectada(s)." % len(real_outs) if real_outs
            else "No se detecta ninguna salida de audio.",
            "kind": "sound",
        })
    else:
        items.append({"title": "Micrófono virtual (VB-CABLE)", "ok": None,
                      "detail": "Se comprobará cuando instales el paquete «sounddevice».",
                      "kind": "cable"})
    return items


CABLE_STEPS = (
    "Cómo instalar el micrófono virtual (VB-CABLE)\n"
    "1. Pulsa «Descargar VB-CABLE» y baja el .zip para Windows.\n"
    "2. Extrae el .zip completo en una carpeta (no lo ejecutes desde dentro del zip).\n"
    "3. Clic derecho en VBCABLE_Setup_x64.exe → «Ejecutar como administrador».\n"
    "4. Pulsa «Install Driver» y acepta el aviso de Windows.\n"
    "5. Reinicia el PC y vuelve a abrir esta app.\n"
)

USAGE_STEPS = (
    "Cómo usarlo en Discord o en el juego\n"
    "1. En Discord: Ajustes → Voz y video → Dispositivo de entrada → "
    "«CABLE Output (VB-Audio Virtual Cable)».\n"
    "2. En Discord desactiva «Supresión de ruido» (Krisp) y «Cancelación de eco»; "
    "si no, filtra los audios como si fueran ruido.\n"
    "3. Deja tus audífonos normales como dispositivo de salida en Discord y en Windows.\n"
    "4. No pongas «CABLE Input» como salida predeterminada de Windows.\n"
)


# =========================================================================
#  Control local (Stream Deck y similares)
# =========================================================================
class ControlServer:
    """Servidor HTTP minimo, solo accesible desde este mismo PC (127.0.0.1)."""

    def __init__(self, handler):
        self.handler = handler      # funcion(path) -> (codigo, texto)
        self.httpd = None
        self.port = None
        self.error = ""

    def start(self, port):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        handle = self.handler

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                try:
                    code, text = handle(self.path)
                except Exception as exc:
                    code, text = 500, "error: %s" % exc
                body = text.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        last = None
        for candidate in range(int(port), int(port) + 10):
            try:
                self.httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
                self.httpd.daemon_threads = True
                self.port = candidate
                break
            except OSError as exc:
                last = exc
        if self.httpd is None:
            self.error = str(last)
            return False
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return True

    def stop(self):
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None


# =========================================================================
#  Interfaz
# =========================================================================
class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.engine = AudioEngine()
        self.cache = {}            # ruta -> audio decodificado
        self.loading = set()
        self.uiq = queue.Queue()   # funciones a ejecutar en el hilo de la interfaz
        self.devs = {"in": [], "out": []}
        self.req_win = None
        self.capture_token = 0
        self.tray = None
        self.tray_notified = False
        self.last_trigger = {}
        self.sd_win = None
        self.server = ControlServer(self.handle_api)
        self._building = True

        root.title(APP_NAME)
        root.geometry("860x680")
        root.minsize(760, 560)
        root.configure(bg=BG)
        self._style()
        self._build()
        self._building = False

        missing = load_modules()
        self.refresh_devices(restart=True)
        self.rebuild_sounds()
        self.register_hotkeys()
        self.preload_all()
        self.setup_tray()
        self.server.start(self.cfg.get("api_port", 47800))
        save_config(self.cfg)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(40, self._poll)
        root.after(120, self._tick)
        if missing or not all(i["ok"] for i in check_requirements(self.cfg) if i["ok"] is not None):
            root.after(300, self.open_requirements)

    # ---- estilo ----------------------------------------------------------
    def _style(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        font = ("Segoe UI", 10) if IS_WINDOWS else ("DejaVu Sans", 10)
        self.font = font
        self.font_b = (font[0], 10, "bold")
        self.font_h = (font[0], 15, "bold")
        st.configure(".", background=BG, foreground=FG, font=font, borderwidth=0)
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("Row.TFrame", background=PANEL2)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Panel.TLabel", background=PANEL, foreground=FG)
        st.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        st.configure("MutedBg.TLabel", background=BG, foreground=MUTED)
        st.configure("H.TLabel", background=BG, foreground=FG, font=self.font_h)
        st.configure("TButton", background=PANEL2, foreground=FG, padding=(10, 6), relief="flat")
        st.map("TButton", background=[("active", "#383c4d"), ("disabled", PANEL)],
               foreground=[("disabled", MUTED)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#0c1020", font=self.font_b)
        st.map("Accent.TButton", background=[("disabled", PANEL2), ("active", "#8aa3ff")],
               foreground=[("disabled", MUTED)])
        st.configure("Danger.TButton", background="#c94a4a", foreground="#ffffff", font=self.font_b)
        st.map("Danger.TButton", background=[("active", "#e06060")])
        st.configure("Play.TButton", background="#343849", foreground=FG, anchor="w",
                     padding=(12, 8), font=self.font_b)
        st.map("Play.TButton", background=[("active", ACCENT)], foreground=[("active", "#0c1020")])
        st.configure("Small.TButton", padding=(8, 8), background=PANEL2)
        st.configure("TCheckbutton", background=PANEL, foreground=FG)
        st.map("TCheckbutton", background=[("active", PANEL)],
               indicatorcolor=[("selected", ACCENT), ("!selected", PANEL2)])
        st.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2, foreground=FG,
                     arrowcolor=FG, padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL2)], foreground=[("readonly", FG)],
               selectbackground=[("readonly", PANEL2)], selectforeground=[("readonly", FG)])
        st.configure("Horizontal.TScale", background=PANEL, troughcolor=PANEL2)
        st.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=BG, arrowcolor=FG,
                     bordercolor=BG, lightcolor=PANEL2, darkcolor=PANEL2)
        st.map("Vertical.TScrollbar", background=[("active", "#383c4d"), ("disabled", BG)])
        st.configure("Horizontal.TProgressbar", background=OK, troughcolor=PANEL2)
        self.root.option_add("*TCombobox*Listbox.background", PANEL2)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.font", font)

    # ---- construccion ----------------------------------------------------
    def _build(self):
        root = self.root
        top = ttk.Frame(root)
        top.pack(fill="x", padx=16, pady=(14, 8))
        ttk.Label(top, text=APP_NAME, style="H.TLabel").pack(side="left")
        ttk.Button(top, text="Salir", command=self.quit).pack(side="right")
        ttk.Button(top, text="■  Detener todo", style="Danger.TButton",
                   command=self.stop_all).pack(side="right", padx=(0, 8))
        ttk.Button(top, text="Stream Deck", command=self.open_streamdeck).pack(side="right", padx=(0, 8))
        ttk.Button(top, text="Requisitos", command=self.open_requirements).pack(side="right", padx=8)

        self.status_var = tk.StringVar(value="Iniciando…")
        self.status_lbl = tk.Label(root, textvariable=self.status_var, bg=BG, fg=MUTED,
                                   font=self.font, anchor="w", justify="left", wraplength=820)
        self.status_lbl.pack(fill="x", padx=16)

        # --- panel de dispositivos y volumenes ---
        panel = ttk.Frame(root, style="Panel.TFrame", padding=12)
        panel.pack(fill="x", padx=16, pady=10)
        panel.columnconfigure(1, weight=1)
        panel.columnconfigure(3, weight=1)

        self.mic_var = tk.StringVar()
        self.cable_var = tk.StringVar()
        self.mon_var = tk.StringVar()
        self.api_var = tk.StringVar(value=self.cfg.get("hostapi", ""))

        def combo(row, col, label, var):
            ttk.Label(panel, text=label, style="Muted.TLabel").grid(
                row=row, column=col, sticky="w", padx=(0, 8), pady=4)
            cb = ttk.Combobox(panel, textvariable=var, state="readonly", width=30)
            cb.grid(row=row, column=col + 1, sticky="ew", padx=(0, 16), pady=4)
            cb.bind("<<ComboboxSelected>>", self.on_device_change)
            return cb

        self.mic_cb = combo(0, 0, "Mi micrófono", self.mic_var)
        self.mon_cb = combo(0, 2, "Yo escucho por", self.mon_var)
        self.cable_cb = combo(1, 0, "Micrófono virtual", self.cable_var)
        self.api_cb = combo(1, 2, "Sistema de audio", self.api_var)

        self.vol_others = tk.DoubleVar(value=self.cfg["vol_others"])
        self.vol_me = tk.DoubleVar(value=self.cfg["vol_me"])
        self.vol_mic = tk.DoubleVar(value=self.cfg["vol_mic"])

        def slider(row, col, label, var):
            ttk.Label(panel, text=label, style="Muted.TLabel").grid(
                row=row, column=col, sticky="w", padx=(0, 8), pady=4)
            box = ttk.Frame(panel, style="Panel.TFrame")
            box.grid(row=row, column=col + 1, sticky="ew", padx=(0, 16), pady=4)
            box.columnconfigure(0, weight=1)
            val = ttk.Label(box, text="", style="Panel.TLabel", width=5, anchor="e")
            val.grid(row=0, column=1, padx=(6, 0))

            def changed(_=None):
                val.configure(text="%d%%" % round(var.get()))
                self.on_volume_change()
            ttk.Scale(box, from_=0, to=150, variable=var, command=changed).grid(
                row=0, column=0, sticky="ew")
            val.configure(text="%d%%" % round(var.get()))

        slider(2, 0, "Volumen para los demás", self.vol_others)
        slider(2, 2, "Volumen para mí", self.vol_me)
        slider(3, 0, "Volumen de mi voz", self.vol_mic)

        voice = ttk.Frame(panel, style="Panel.TFrame")
        voice.grid(row=3, column=2, columnspan=2, sticky="ew", pady=4)
        self.mic_on = tk.BooleanVar(value=bool(self.cfg["mic_enabled"]))
        ttk.Checkbutton(voice, text="Enviar mi voz", variable=self.mic_on,
                        command=self.on_mic_toggle).pack(side="left")
        self.meter = ttk.Progressbar(voice, length=110, maximum=100, mode="determinate")
        self.meter.pack(side="left", padx=10)

        keys = ttk.Frame(panel, style="Panel.TFrame")
        keys.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(keys, text="Atajos generales:", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self.hk_stop_btn = ttk.Button(keys, command=lambda: self.capture_hotkey("hk_stop"))
        self.hk_stop_btn.pack(side="left", padx=(0, 8))
        self.hk_mic_btn = ttk.Button(keys, command=lambda: self.capture_hotkey("hk_mic"))
        self.hk_mic_btn.pack(side="left")
        self._label_global_keys()

        # --- lista de sonidos ---
        head = ttk.Frame(root)
        head.pack(fill="x", padx=16, pady=(4, 6))
        ttk.Label(head, text="Mis audios", font=self.font_b).pack(side="left")
        ttk.Button(head, text="+  Agregar audios (.mp3, .wav…)", style="Accent.TButton",
                   command=self.add_sounds).pack(side="right")

        wrap = ttk.Frame(root)
        wrap.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list_frame = ttk.Frame(self.canvas)
        self.list_win = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>",
                             lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self.list_win, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _wheel(self, event):
        if self.list_frame.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    # ---- utilidades de interfaz -------------------------------------------
    def set_status(self, text, color=MUTED):
        self.status_var.set(text)
        self.status_lbl.configure(fg=color)

    def _poll(self):
        try:
            while True:
                fn = self.uiq.get_nowait()
                try:
                    fn()
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.root.after(40, self._poll)

    def _tick(self):
        level = self.engine.mic_level if self.mic_on.get() else 0.0
        self.engine.mic_level *= 0.7
        self.meter["value"] = min(100, level * 140)
        self.root.after(120, self._tick)

    def save(self):
        if self._building:
            return
        self.cfg["vol_others"] = round(self.vol_others.get())
        self.cfg["vol_me"] = round(self.vol_me.get())
        self.cfg["vol_mic"] = round(self.vol_mic.get())
        self.cfg["mic_enabled"] = bool(self.mic_on.get())
        self.cfg["mic_device"] = self.mic_var.get()
        self.cfg["cable_device"] = self.cable_var.get()
        self.cfg["monitor_device"] = self.mon_var.get()
        self.cfg["hostapi"] = self.api_var.get()
        save_config(self.cfg)

    # ---- dispositivos -----------------------------------------------------
    NONE = "(ninguno)"

    def refresh_devices(self, restart=False, rescan=False):
        if rescan:
            self.engine.stop_streams()
            rescan_devices()
        apis = hostapi_names()
        self.api_cb["values"] = apis
        if apis and self.api_var.get() not in apis:
            self.api_var.set("Windows WASAPI" if "Windows WASAPI" in apis else apis[0])
        ins, outs, def_in, def_out = list_devices(self.api_var.get())
        self.devs = {"in": ins, "out": outs}
        mics = [d["name"] for d in ins if not is_virtual(d["name"])]
        mons = [d["name"] for d in outs if not is_virtual(d["name"])]
        cables = [d["name"] for d in outs]
        self.mic_cb["values"] = [self.NONE] + mics
        self.mon_cb["values"] = [self.NONE] + mons
        self.cable_cb["values"] = [self.NONE] + cables

        def pick(saved, options, default):
            if saved == self.NONE:
                return saved
            if saved in options:
                return saved
            if default in options:
                return default
            return options[0] if options else self.NONE

        auto_cable = next((n for n in cables if "cable input" in n.lower()), self.NONE)
        saved_cable = self.cfg.get("cable_device", "")
        self.mic_var.set(pick(self.cfg.get("mic_device", ""), mics, def_in))
        self.mon_var.set(pick(self.cfg.get("monitor_device", ""), mons, def_out))
        self.cable_var.set(saved_cable if saved_cable in cables else auto_cable)
        if restart or rescan:
            self.restart_engine()

    def _dev(self, kind, name):
        return next((d for d in self.devs[kind] if d["name"] == name), None)

    def restart_engine(self):
        self.on_volume_change(save=False)
        self.engine.mic_enabled = bool(self.mic_on.get())
        if sd is None or np is None:
            self.set_status("●  Faltan paquetes por instalar. Abre «Requisitos».", BAD)
            return
        cable = self._dev("out", self.cable_var.get())
        mon = self._dev("out", self.mon_var.get())
        mic = self._dev("in", self.mic_var.get())
        errors = self.engine.start(mic, cable, mon)
        if errors:
            hint = ""
            if "WASAPI" in self.api_var.get():
                hint = "  Prueba cambiar «Sistema de audio» a MME."
            self.set_status("●  " + "  |  ".join(errors) + hint, BAD)
        elif cable is None:
            self.set_status("●  No hay micrófono virtual: solo tú escucharás los audios. "
                            "Abre «Requisitos» para instalarlo.", WARN)
        else:
            voz = "voz + audios" if (self.mic_on.get() and self.engine.mic_stream) else "solo audios"
            self.set_status("●  Micrófono virtual activo (%s). En Discord o el juego elige "
                            "«CABLE Output» como micrófono." % voz, OK)
        self.save()

    def on_device_change(self, _=None):
        self.cfg["mic_device"] = self.mic_var.get()
        self.cfg["cable_device"] = self.cable_var.get()
        self.cfg["monitor_device"] = self.mon_var.get()
        if self.cfg.get("hostapi") != self.api_var.get():
            self.cfg["hostapi"] = self.api_var.get()
            self.refresh_devices()
        self.restart_engine()
        self.root.focus_set()

    def on_volume_change(self, save=True):
        self.engine.vol_others = self.vol_others.get() / 100.0
        self.engine.vol_me = self.vol_me.get() / 100.0
        self.engine.vol_mic = self.vol_mic.get() / 100.0
        if save:
            self.save()

    def on_mic_toggle(self):
        self.engine.mic_enabled = bool(self.mic_on.get())
        if self.engine.cable_stream is not None:
            voz = "voz + audios" if (self.mic_on.get() and self.engine.mic_stream) else "solo audios"
            self.set_status("●  Micrófono virtual activo (%s). En Discord o el juego elige "
                            "«CABLE Output» como micrófono." % voz, OK)
        self.save()

    def toggle_mic(self):
        self.mic_on.set(not self.mic_on.get())
        self.on_mic_toggle()

    # ---- sonidos ----------------------------------------------------------
    def rebuild_sounds(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        sounds = self.cfg["sounds"]
        if not sounds:
            ttk.Label(self.list_frame, style="MutedBg.TLabel", justify="center",
                      text="Todavía no hay audios.\nPulsa «Agregar audios» y elige tus .mp3.").pack(pady=40)
            return
        for snd in sounds:
            row = ttk.Frame(self.list_frame, style="Row.TFrame", padding=6)
            row.pack(fill="x", pady=3)
            row.columnconfigure(0, weight=1)
            exists = os.path.isfile(snd["path"])
            name = "▶  " + snd["name"] + ("" if exists else "   (archivo no encontrado)")
            ttk.Button(row, text=name, style="Play.TButton",
                       command=lambda s=snd: self.play(s)).grid(row=0, column=0, sticky="ew")
            hk = snd.get("hotkey") or ""
            ttk.Button(row, text=("⌨  " + hk) if hk else "⌨  Sin atajo", width=18,
                       style="Small.TButton",
                       command=lambda s=snd: self.capture_hotkey(s)).grid(row=0, column=1, padx=(6, 0))
            ttk.Button(row, text="Renombrar", style="Small.TButton",
                       command=lambda s=snd: self.rename(s)).grid(row=0, column=2, padx=(6, 0))
            ttk.Button(row, text="Quitar", style="Small.TButton",
                       command=lambda s=snd: self.remove(s)).grid(row=0, column=3, padx=(6, 0))

    def add_sounds(self):
        paths = filedialog.askopenfilenames(
            parent=self.root, title="Elige tus audios",
            filetypes=[("Audio", "*.mp3 *.wav *.ogg *.flac"), ("Todos los archivos", "*.*")])
        known = {s["path"] for s in self.cfg["sounds"]}
        for p in paths:
            p = os.path.abspath(p)
            if p in known:
                continue
            name = os.path.splitext(os.path.basename(p))[0]
            self.cfg["sounds"].append({"name": name, "path": p, "hotkey": "",
                                       "id": secrets.token_hex(3)})
            self.preload(p)
        if paths:
            self.save()
            self.rebuild_sounds()

    def rename(self, snd):
        new = simpledialog.askstring(APP_NAME, "Nuevo nombre:", initialvalue=snd["name"],
                                     parent=self.root)
        if new and new.strip():
            snd["name"] = new.strip()
            self.save()
            self.rebuild_sounds()

    def remove(self, snd):
        if not messagebox.askyesno(APP_NAME, "¿Quitar «%s» de la lista?\n(El archivo no se borra.)"
                                   % snd["name"], parent=self.root):
            return
        self.cfg["sounds"] = [s for s in self.cfg["sounds"] if s is not snd]
        self.cache.pop(snd["path"], None)
        self.save()
        self.rebuild_sounds()
        self.register_hotkeys()

    def preload_all(self):
        for snd in self.cfg["sounds"]:
            self.preload(snd["path"])

    def preload(self, path, then_play=False):
        """Decodifica el archivo en segundo plano para que suene al instante."""
        if (sf is None and miniaudio is None) or np is None or path in self.cache or path in self.loading:
            return
        if not os.path.isfile(path):
            return
        self.loading.add(path)

        def work():
            try:
                data = decode_file(path)
                err = None
            except Exception as exc:
                data, err = None, str(exc)

            def done():
                self.loading.discard(path)
                if data is not None:
                    self.cache[path] = data
                    if then_play:
                        self._play_data(data)
                else:
                    self.set_status("●  No se pudo leer %s: %s" % (os.path.basename(path), err), BAD)
            self.uiq.put(done)
        threading.Thread(target=work, daemon=True).start()

    def _play_data(self, data):
        if not self.engine.play(data):
            self.uiq.put(lambda: self.set_status(
                "●  No hay ninguna salida de audio activa. Revisa los dispositivos.", BAD))

    def play(self, snd):
        """Puede llamarse desde el hilo de atajos de teclado."""
        if sd is None or (sf is None and miniaudio is None) or np is None:
            self.uiq.put(self.open_requirements)
            return
        data = self.cache.get(snd["path"])
        if data is not None:
            self._play_data(data)
        elif not os.path.isfile(snd["path"]):
            self.uiq.put(lambda: self.set_status(
                "●  No encuentro el archivo: %s" % snd["path"], BAD))
        else:
            self.uiq.put(lambda: self.preload(snd["path"], then_play=True))

    def play_hotkey(self, snd):
        """Si dejas la tecla pulsada, Windows la repite ~30 veces por segundo;
        sin este filtro el audio se dispararia encima de si mismo."""
        now = time.monotonic()
        if now - self.last_trigger.get(snd["path"], 0.0) < 0.25:
            self.last_trigger[snd["path"]] = now
            return
        self.last_trigger[snd["path"]] = now
        self.play(snd)

    def stop_all(self):
        self.engine.stop_all()

    # ---- atajos de teclado -------------------------------------------------
    def _label_global_keys(self):
        self.hk_stop_btn.configure(text="Detener todo: " + (self.cfg.get("hk_stop") or "sin atajo"))
        self.hk_mic_btn.configure(text="Activar/quitar mi voz: " + (self.cfg.get("hk_mic") or "sin atajo"))

    def _unhook(self):
        if kb is None:
            return
        try:
            kb.unhook_all_hotkeys()
        except Exception:
            pass

    def register_hotkeys(self):
        if kb is None:
            return
        self._unhook()
        failed = []

        def add(combo, fn):
            if not combo:
                return
            try:
                kb.add_hotkey(combo, fn)
            except Exception:
                failed.append(combo)

        for snd in self.cfg["sounds"]:
            add(snd.get("hotkey"), lambda s=snd: self.play_hotkey(s))
        add(self.cfg.get("hk_stop"), self.stop_all)
        add(self.cfg.get("hk_mic"), lambda: self.uiq.put(self.toggle_mic))
        if failed:
            self.set_status("●  No se pudieron activar estos atajos: " + ", ".join(failed), WARN)

    def capture_hotkey(self, target):
        """target: un sonido (dict) o el nombre de un atajo general (str)."""
        if kb is None:
            messagebox.showinfo(APP_NAME, "Para usar atajos falta el paquete «keyboard».\n"
                                          "Instálalo desde «Requisitos».", parent=self.root)
            return
        self.capture_token += 1
        token = self.capture_token
        self._unhook()

        win = tk.Toplevel(self.root)
        win.title("Asignar atajo")
        win.configure(bg=PANEL, padx=24, pady=18)
        win.transient(self.root)
        win.resizable(False, False)
        ttk.Label(win, text="Pulsa la tecla o combinación…", style="Panel.TLabel",
                  font=self.font_b).pack(pady=(0, 4))
        ttk.Label(win, text="Ejemplos: F8, Ctrl+1, Alt+Q. Las teclas del teclado numérico\n"
                            "van bien porque no estorban en el juego.",
                  style="Muted.TLabel", justify="center").pack(pady=(0, 12))
        btns = ttk.Frame(win, style="Panel.TFrame")
        btns.pack()

        def apply(combo):
            if isinstance(target, str):
                self.cfg[target] = combo
                self._label_global_keys()
            else:
                target["hotkey"] = combo
                self.rebuild_sounds()
            self.save()

        def finish(combo, from_key=False):
            if token != self.capture_token:
                return  # ya se cerro esta captura
            self.capture_token += 1
            if combo is not None and not (from_key and combo in ("esc", "escape")):
                apply(combo)
            try:
                win.destroy()
            except Exception:
                pass
            self.register_hotkeys()

        ttk.Button(btns, text="Quitar atajo", command=lambda: finish("")).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancelar", command=lambda: finish(None)).pack(side="left", padx=4)
        win.protocol("WM_DELETE_WINDOW", lambda: finish(None))

        def worker():
            try:
                combo = kb.read_hotkey(suppress=False)
            except Exception:
                combo = None
            self.uiq.put(lambda: finish(combo, from_key=True))
        threading.Thread(target=worker, daemon=True).start()

    # ---- Stream Deck ---------------------------------------------------------
    def handle_api(self, path):
        """Se ejecuta en el hilo del servidor. Devuelve (codigo, texto)."""
        url = urllib.parse.urlsplit(path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        route = url.path.rstrip("/").lower()
        if route in ("", "/ping"):
            return 200, "Soundboard Mic"
        if q.get("k") != self.cfg.get("api_token"):
            return 403, "clave incorrecta"
        if route == "/play":
            snd = None
            if q.get("id"):
                snd = next((x for x in self.cfg["sounds"] if x.get("id") == q["id"]), None)
            elif q.get("name"):
                want = q["name"].strip().lower()
                snd = next((x for x in self.cfg["sounds"] if x["name"].strip().lower() == want), None)
            if snd is None:
                return 404, "audio no encontrado"
            self.play(snd)
            return 200, "ok: " + snd["name"]
        if route == "/stop":
            self.stop_all()
            return 200, "ok: detenido"
        if route == "/mic":
            state = q.get("state", "toggle").lower()
            if state == "toggle":
                self.uiq.put(self.toggle_mic)
            elif state in ("on", "off"):
                self.uiq.put(lambda: (self.mic_on.set(state == "on"), self.on_mic_toggle()))
            else:
                return 400, "state debe ser toggle, on u off"
            return 200, "ok: voz " + state
        return 404, "ruta desconocida"

    def api_url(self, route, **params):
        params["k"] = self.cfg["api_token"]
        return "http://127.0.0.1:%s/%s?%s" % (self.server.port, route, urllib.parse.urlencode(params))

    def copy(self, text, label):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        if self.sd_win is not None and self.sd_win.winfo_exists():
            self.sd_msg.configure(text="Copiado: " + label, fg=OK)

    def open_streamdeck(self):
        if self.sd_win is not None and self.sd_win.winfo_exists():
            self.sd_win.destroy()
        win = tk.Toplevel(self.root)
        self.sd_win = win
        win.title("Stream Deck")
        win.configure(bg=BG)
        win.geometry("640x620")
        win.transient(self.root)
        ttk.Label(win, text="Stream Deck", style="H.TLabel").pack(anchor="w", padx=16, pady=(14, 4))

        if self.server.port is None:
            tk.Label(win, bg=BG, fg=BAD, font=self.font, justify="left", wraplength=600, anchor="w",
                     text="No se pudo activar el control local (%s). Puedes usar igualmente los "
                          "atajos de teclado con la acción «Hotkey» del Stream Deck."
                          % self.server.error).pack(fill="x", padx=16, pady=8)
            return
        steps = (
            "Cada audio tiene un enlace. Al pulsar la tecla del Stream Deck, este llama al enlace "
            "y el audio suena, aunque la app esté oculta junto al reloj.\n\n"
            "1. En el programa de Stream Deck arrastra la acción «System → Website» a una tecla.\n"
            "2. Aquí pulsa «Copiar enlace» en el audio que quieras y pégalo en el campo URL.\n"
            "3. Marca la casilla «GET request in background» para que no abra el navegador.\n\n"
            "Otra opción: la acción «Hotkey» del Stream Deck con el atajo que asignaste al audio."
        )
        tk.Label(win, text=steps, bg=PANEL, fg=FG, font=self.font, justify="left", anchor="w",
                 wraplength=580, padx=12, pady=10).pack(fill="x", padx=16, pady=(4, 8))
        self.sd_msg = tk.Label(win, text="Los enlaces solo funcionan desde este PC y con la app abierta.",
                               bg=BG, fg=MUTED, font=self.font, anchor="w")
        self.sd_msg.pack(fill="x", padx=16)

        wrap = ttk.Frame(win)
        wrap.pack(fill="both", expand=True, padx=16, pady=10)
        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        sb_ = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb_.set)
        sb_.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas)
        item = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))

        def row(label, url):
            fr = ttk.Frame(inner, style="Row.TFrame", padding=6)
            fr.pack(fill="x", pady=2)
            tk.Label(fr, text=label, bg=PANEL2, fg=FG, font=self.font_b, anchor="w").pack(
                side="left", fill="x", expand=True, padx=6)
            ttk.Button(fr, text="Copiar enlace", style="Small.TButton",
                       command=lambda: self.copy(url, label)).pack(side="right")

        row("■  Detener todo", self.api_url("stop"))
        row("Activar/quitar mi voz", self.api_url("mic", state="toggle"))
        for snd in self.cfg["sounds"]:
            row("▶  " + snd["name"], self.api_url("play", id=snd["id"]))

    # ---- ventana de requisitos ----------------------------------------------
    def open_requirements(self):
        if self.req_win is not None and self.req_win.winfo_exists():
            self.req_win.lift()
            self._fill_requirements()
            return
        win = tk.Toplevel(self.root)
        self.req_win = win
        win.title("Requisitos — qué tienes y qué falta")
        win.configure(bg=BG)
        win.geometry("720x700")
        win.transient(self.root)

        ttk.Label(win, text="Requisitos", style="H.TLabel").pack(anchor="w", padx=16, pady=(14, 2))
        self.req_summary = tk.Label(win, text="", bg=BG, fg=MUTED, font=self.font, anchor="w")
        self.req_summary.pack(fill="x", padx=16)

        self.req_list = ttk.Frame(win)
        self.req_list.pack(fill="x", padx=16, pady=10)

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=16)
        self.btn_pip = ttk.Button(bar, text="Instalar lo que falta", style="Accent.TButton",
                                  command=self.install_packages)
        self.btn_pip.pack(side="left")
        ttk.Button(bar, text="Descargar VB-CABLE",
                   command=lambda: webbrowser.open(VBCABLE_URL)).pack(side="left", padx=6)
        ttk.Button(bar, text="Sonido de Windows", command=self.open_sound_settings).pack(side="left")
        ttk.Button(bar, text="Volver a comprobar", command=self.recheck).pack(side="right")

        self.req_text = tk.Text(win, bg=PANEL, fg=FG, font=self.font, wrap="word", height=12,
                                relief="flat", padx=12, pady=10, highlightthickness=0)
        self.req_text.pack(fill="both", expand=True, padx=16, pady=12)
        self._fill_requirements()

    def _set_req_text(self, text):
        self.req_text.configure(state="normal")
        self.req_text.delete("1.0", "end")
        self.req_text.insert("1.0", text)
        self.req_text.configure(state="disabled")

    def _fill_requirements(self):
        for w in self.req_list.winfo_children():
            w.destroy()
        items = check_requirements(self.cfg)
        for it in items:
            row = ttk.Frame(self.req_list, style="Panel.TFrame", padding=(10, 4))
            row.pack(fill="x", pady=2)
            if it["ok"] is None:
                mark, color = "?", MUTED
            elif it["ok"]:
                mark, color = "✔", OK
            else:
                mark, color = "✖", BAD
            tk.Label(row, text=mark, fg=color, bg=PANEL, font=(self.font[0], 13, "bold"),
                     width=2).pack(side="left")
            box = ttk.Frame(row, style="Panel.TFrame")
            box.pack(side="left", fill="x", expand=True)
            if it["ok"]:
                # lo que ya esta listo ocupa una sola linea
                ttk.Label(box, text=it["title"], style="Panel.TLabel",
                          font=self.font_b).pack(side="left")
                ttk.Label(box, text="  " + it["detail"], style="Muted.TLabel").pack(side="left")
            else:
                ttk.Label(box, text=it["title"], style="Panel.TLabel",
                          font=self.font_b).pack(anchor="w")
                ttk.Label(box, text=it["detail"], style="Muted.TLabel", wraplength=600,
                          justify="left").pack(anchor="w")
        pending = [i for i in items if i["ok"] is False]
        need_pip = any(i["kind"] == "pip" for i in pending)
        self.btn_pip.configure(state="normal" if need_pip else "disabled")
        if not pending and all(i["ok"] for i in items):
            self.req_summary.configure(text="Todo listo. Ya puedes usar el tablero.", fg=OK)
        else:
            self.req_summary.configure(
                text="Faltan %d cosa(s). Sigue el orden de la lista, de arriba hacia abajo."
                     % len([i for i in items if not i["ok"]]), fg=WARN)
        self._set_req_text(CABLE_STEPS + "\n" + USAGE_STEPS)

    def recheck(self):
        load_modules()
        self.refresh_devices(rescan=True)
        self.register_hotkeys()
        self.preload_all()
        self.rebuild_sounds()
        self.setup_tray()
        if self.req_win is not None and self.req_win.winfo_exists():
            self._fill_requirements()

    def open_sound_settings(self):
        try:
            if IS_WINDOWS:
                os.startfile("ms-settings:sound")
            else:
                messagebox.showinfo(APP_NAME, "Esta opción solo funciona en Windows.",
                                    parent=self.req_win or self.root)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.req_win or self.root)

    def install_packages(self):
        self.btn_pip.configure(state="disabled")
        self._set_req_text("Instalando paquetes… puede tardar un par de minutos.\n")
        exe = sys.executable
        if os.path.basename(exe).lower() == "pythonw.exe":
            alt = os.path.join(os.path.dirname(exe), "python.exe")
            if os.path.isfile(alt):
                exe = alt
        cmd = [exe, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check"]
        cmd += [p[1] for p in PACKAGES]

        def work():
            try:
                flags = 0x08000000 if IS_WINDOWS else 0  # sin ventana de consola
                proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                                      creationflags=flags)
                out = (proc.stdout or "") + (proc.stderr or "")
                ok = proc.returncode == 0
            except Exception as exc:
                out, ok = str(exc), False

            def done():
                self.recheck()
                if self.req_win is None or not self.req_win.winfo_exists():
                    return
                if ok:
                    msg = "Paquetes instalados correctamente.\n\n"
                else:
                    msg = ("No se pudieron instalar los paquetes. Revisa tu conexión a internet "
                           "o instala a mano con:\n    " + " ".join(cmd[1:]).replace("-m pip", "py -m pip", 1)
                           + "\n\nDetalle:\n" + out[-1500:] + "\n\n")
                self._set_req_text(msg + CABLE_STEPS + "\n" + USAGE_STEPS)
            self.uiq.put(done)
        threading.Thread(target=work, daemon=True).start()

    # ---- segundo plano y cierre -------------------------------------------------
    def setup_tray(self):
        """Icono junto al reloj de Windows. Si faltan paquetes, no hace nada."""
        if self.tray is not None or pystray is None or pil is None:
            return
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(108, 140, 255, 255))
            draw.polygon([(24, 16), (24, 48), (50, 32)], fill=(12, 16, 32, 255))
            menu = pystray.Menu(
                pystray.MenuItem("Abrir " + APP_NAME,
                                 lambda: self.uiq.put(self.show_window), default=True),
                pystray.MenuItem("Detener todo", lambda: self.stop_all()),
                pystray.MenuItem("Activar/quitar mi voz", lambda: self.uiq.put(self.toggle_mic)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Salir", lambda: self.uiq.put(self.quit)),
            )
            tray = pystray.Icon("soundboard_mic", img, APP_NAME, menu)
            tray.run_detached()
            self.tray = tray
        except Exception:
            self.tray = None

    def on_close(self):
        """La X no cierra la app: la deja funcionando en segundo plano."""
        self.save()
        self.setup_tray()
        if self.tray is not None:
            self.root.withdraw()
            if not self.tray_notified:
                self.tray_notified = True
                try:
                    self.tray.notify("Sigo funcionando en segundo plano. Doble clic en el icono "
                                     "para abrirme; clic derecho → Salir para cerrarme.", APP_NAME)
                except Exception:
                    pass
        else:
            self.root.iconify()
            self.set_status("●  Minimizada, sigue funcionando. Para ocultarla junto al reloj instala "
                            "lo que falta en «Requisitos». Usa «Salir» para cerrarla del todo.", WARN)

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass

    def quit(self):
        self.save()
        self._unhook()
        self.engine.stop_streams()
        self.server.stop()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()


def already_running():
    """Evita abrir dos copias (la primera puede estar oculta junto al reloj)."""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        global _MUTEX
        _MUTEX = k32.CreateMutexW(None, 0, "SoundboardMic_instancia_unica")
        return ctypes.get_last_error() == 183   # ERROR_ALREADY_EXISTS
    except Exception:
        return False


def boost_priority():
    """Da prioridad al audio para que la voz no se corte cuando el juego usa mucha CPU."""
    try:
        sys.setswitchinterval(0.001)
    except Exception:
        pass
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00000080)   # HIGH_PRIORITY_CLASS
    except Exception:
        pass


def main():
    root = tk.Tk()
    if already_running():
        root.withdraw()
        messagebox.showinfo(APP_NAME, "Soundboard Mic ya está abierta.\n\n"
                            "Búscala en los iconos junto al reloj de Windows (puede estar "
                            "en la flecha «^») y haz doble clic para abrirla.")
        root.destroy()
        return
    boost_priority()
    try:
        App(root)
    except Exception:
        err = traceback.format_exc()
        try:
            with open(LOG_PATH, "w", encoding="utf-8") as fh:
                fh.write(err)
        except Exception:
            pass
        messagebox.showerror(APP_NAME, "La app no pudo iniciar:\n\n" + err[-1200:])
        root.destroy()
        return

    def report(exc, val, tb):
        err = "".join(traceback.format_exception(exc, val, tb))
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(err + "\n")
        except Exception:
            pass
        messagebox.showerror(APP_NAME, "Ocurrió un error:\n\n" + err[-1200:])
    root.report_callback_exception = report
    root.mainloop()


if __name__ == "__main__":
    main()
