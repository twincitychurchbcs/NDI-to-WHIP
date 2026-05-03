#!/usr/bin/env python3
"""
ndi_to_whip.py — Production NDI → WHIP bridge

Captures a named NDI source via GStreamer and publishes it to a
WHIP (WebRTC HTTP Ingest Protocol) endpoint with structured logging,
retry/reconnect logic, and graceful shutdown.

Requirements:
  - GStreamer 1.20+
  - gst-plugin-ndi   (libgstndi.so   from gst-plugins-rs)
  - gst-plugin-webrtc (libgstrswebrtc.so from gst-plugins-rs, provides whipclientsink)
  - PyGObject (gi), tomli/tomllib

Usage:
  python ndi_to_whip.py --config /etc/ndi_to_whip/config.toml
  python ndi_to_whip.py --probe               # list visible NDI sources
  python ndi_to_whip.py --print-pipeline      # print GStreamer pipeline string
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from collections import deque
import statistics

# ── Python 3.11+ has tomllib in stdlib; older Pythons need tomli ─────────────
try:
    import tomllib                  # type: ignore[import]
except ImportError:
    try:
        import tomli as tomllib     # type: ignore[import]
    except ImportError:
        tomllib = None              # type: ignore[assignment]

# ── GStreamer ───────────────────────────────────────────────────────────[...]
try:
    import gi
    gi.require_version("Gst",    "1.0")
    gi.require_version("GLib",   "2.0")
    gi.require_version("GstWebRTC", "1.0")
    from gi.repository import Gst, GLib, GstWebRTC   # noqa: F401
except Exception as exc:
    sys.exit(f"[FATAL] GStreamer Python bindings not available: {exc}")


# ── Structured logging ────────────────────────────────────────────────────────
try:
    import structlog
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        logger_factory=structlog.PrintLoggerFactory(),
    )
    log = structlog.get_logger("ndi_to_whip")
except ImportError:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    log = logging.getLogger("ndi_to_whip")  # type: ignore[assignment]


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class Config:
    # NDI source
    ndi_source_name: str          = "MYCOMPUTER (Stream 1)"
    backup_ndi_source_name: str   = ""      # optional fallback NDI source name
    ndi_connect_timeout_ms: int   = 15000   # ms to wait for NDI source on startup

    # WHIP endpoint
    whip_url: str                 = "https://your-whip-endpoint/whip"
    auth_token: str               = ""      # Bearer token (empty = no auth header)
    stun_server: str              = "stun://stun.l.google.com:19302"
    turn_server: str              = ""      # e.g. "turn://user:pass@turn.example.com"

    # Video
    video_width: int              = 1920
    video_height: int             = 1080
    video_framerate: int          = 30
    video_bitrate_kbps: int       = 4000    # kbps; sets start/min/max-bitrate on whipclientsink
    video_encoder: str            = "x264"  # informational; GstBaseWebRTCSink selects encoder via codec discovery
    video_keyframe_interval: int  = 0       # 0 = 2× framerate

    # Audio
    audio_channels: int           = 2
    audio_sample_rate: int        = 48000   # Hz
    audio_bitrate_bps: int        = 128000  # bps for Opus encoder

    # Retry / reconnect
    retry_max_attempts: int       = 0       # 0 = unlimited
    retry_initial_delay_s: float  = 2.0
    retry_max_delay_s: float      = 60.0
    retry_backoff_factor: float   = 2.0

    # Logging
    log_level: str                = "INFO"
    log_gst_debug: str            = "2"     # GST_DEBUG level (0–9)

    @property
    def keyframe_interval(self) -> int:
        return self.video_keyframe_interval or (self.video_framerate * 2)


def load_config(config_path: Optional[str], overrides: dict) -> Config:
    """
    Load config from a TOML file (optional) then apply CLI overrides.
    TOML is grouped under [ndi], [whip], [video], [audio], [retry], [logging].
    """
    cfg = Config()

    if config_path:
        path = Path(config_path)
        if not path.exists():
            log.warning("config_file_not_found", path=str(path))
        else:
            if tomllib is None:
                log.warning(
                    "toml_not_available",
                    hint="pip install tomli  (Python < 3.11)",
                )
            else:
                mode = "rb" if hasattr(tomllib, "load") else "rb"
                with open(path, "rb") as fh:
                    data = tomllib.load(fh)

                def _apply(section: str, mapping: dict[str, str]) -> None:
                    for toml_key, attr in mapping.items():
                        val = data.get(section, {}).get(toml_key)
                        if val is not None:
                            setattr(cfg, attr, val)

                _apply("ndi",    {
                    "source_name":        "ndi_source_name",
                    "backup_source_name": "backup_ndi_source_name",
                    "connect_timeout_ms": "ndi_connect_timeout_ms",
                })
                _apply("whip",   {
                    "url":          "whip_url",
                    "auth_token":   "auth_token",
                    "stun_server":  "stun_server",
                    "turn_server":  "turn_server",
                })
                _apply("video",  {
                    "width":              "video_width",
                    "height":             "video_height",
                    "framerate":          "video_framerate",
                    "bitrate_kbps":       "video_bitrate_kbps",
                    "encoder":            "video_encoder",
                    "keyframe_interval":  "video_keyframe_interval",
                })
                _apply("audio",  {
                    "channels":     "audio_channels",
                    "sample_rate":  "audio_sample_rate",
                    "bitrate_bps":  "audio_bitrate_bps",
                })
                _apply("retry",  {
                    "max_attempts":    "retry_max_attempts",
                    "initial_delay_s": "retry_initial_delay_s",
                    "max_delay_s":     "retry_max_delay_s",
                    "backoff_factor":  "retry_backoff_factor",
                })
                _apply("logging", {
                    "level":     "log_level",
                    "gst_debug": "log_gst_debug",
                })

    # CLI overrides (only set if not None)
    for attr, val in overrides.items():
        if val is not None:
            setattr(cfg, attr, val)

    return cfg


# =============================================================================
# PIPELINE BUILDER
# =============================================================================

# NOTE: In gst-plugins-rs >= 1.24, whipclientsink (GstBaseWebRTCSink) handles
# codec selection and encoding internally via factory discovery. ENCODER_PROFILES
# is retained for CLI --encoder validation and future use but is not inserted
# into the pipeline string. The active codec is constrained by video-caps="video/x-h264".
ENCODER_PROFILES = {
    "x264": {
        "encoder":  (
            "x264enc name=venc "
            "tune=zerolatency "
            "speed-preset=ultrafast "
            "bitrate={bitrate} "
            "key-int-max={keyframe_interval} "
        ),
        "caps":    "video/x-h264,profile=constrained-baseline",
        "pay":     "rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=102",
    },
    "vaapi": {
        "encoder":  (
            "vaapih264enc name=venc "
            "bitrate={bitrate} "
            "keyframe-period={keyframe_interval} "
            "rate-control=cbr "
            "quality-level=1 "
        ),
        "caps":    "video/x-h264,profile=constrained-baseline",
        "pay":     "rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=102",
    },
    "nvenc": {
        "encoder":  (
            "nvh264enc name=venc "
            "bitrate={bitrate} "
            "gop-size={keyframe_interval} "
            "preset=low-latency-hq "
            "rc-mode=cbr "
        ),
        "caps":    "video/x-h264,profile=constrained-baseline",
        "pay":     "rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=102",
    },
}


def build_pipeline_string(cfg: Config, demux_video_pad: str = "demux.video",
                          demux_audio_pad: str = "demux.audio") -> str:
    """
    Build the GStreamer pipeline string.

    Pipeline topology (gst-plugins-rs >= 1.24):
      ndisrc → ndisrcdemux ─┬─ [video: raw] → whipclientsink
                            └─ [audio: raw] ↗

    whipclientsink (GstBaseWebRTCSink) handles codec negotiation and
    encoding internally. Feed raw video/audio — pre-encoded input is
    not supported by the BaseWebRTCSink codec discovery mechanism.

    ndisrc / ndisrcdemux notes:
      The gst-plugins-rs NDI plugin outputs a raw NDI buffer via ndisrc;
      ndisrcdemux splits it into separate video/audio src pads.
      Run:  gst-inspect-1.0 ndisrcdemux  to see actual pad template names.
    """
    video_caps = (
        f"video/x-raw,format=I420"
        f",width={cfg.video_width}"
        f",height={cfg.video_height}"
        f",framerate={cfg.video_framerate}/1"
    )

    audio_caps = (
        f"audio/x-raw"
        f",rate={cfg.audio_sample_rate}"
        f",channels={cfg.audio_channels}"
        f",format=S16LE"
        f",layout=interleaved"
    )

    # Conditional auth-token property (child property of signaller in 1.24+)
    auth_prop = f'signaller::auth-token="{cfg.auth_token}"' if cfg.auth_token else ""

    # Conditional STUN/TURN
    stun_prop = f'stun-server="{cfg.stun_server}"' if cfg.stun_server else ""
    turn_prop = f'turn-server="{cfg.turn_server}"' if cfg.turn_server else ""

    # Bitrate in bps for GstBaseWebRTCSink (config stores kbps for video)
    video_bps = cfg.video_bitrate_kbps * 1000

    pipeline = f"""
        whipclientsink name=whip
            signaller::whip-endpoint="{cfg.whip_url}"
            {auth_prop}
            {stun_prop}
            {turn_prop}
            video-caps="video/x-h264"
            start-bitrate={video_bps}
            min-bitrate={video_bps}
            max-bitrate={video_bps}
            async-handling=true

        ndisrc name=ndi_src
            ndi-name="{cfg.ndi_source_name}"
            connect-timeout={cfg.ndi_connect_timeout_ms}
            do-timestamp=true
        ! ndisrcdemux name=demux

        {demux_video_pad}
        ! queue name=vqueue
            leaky=downstream
            max-size-buffers=5
            max-size-time=200000000
            max-size-bytes=0
        ! videoconvert
        ! videoscale
        ! videorate
        ! {video_caps}
        ! identity sync=true
        ! whip.

        {demux_audio_pad}
        ! queue name=aqueue
            leaky=downstream
            max-size-buffers=10
            max-size-time=200000000
            max-size-bytes=0
        ! audioconvert
        ! audioresample
        ! adelay name=adel delay=0
        ! {audio_caps}
        ! identity sync=true
        ! whip.
    """

    # Collapse extra whitespace for clean logging
    return " ".join(pipeline.split())


# =============================================================================
# NDI SOURCE PROBE
# =============================================================================

# Long-lived ndideviceprovider instance for reliable repeated probing.
_NDI_PROBE_CTX: Optional[GLib.MainContext] = None
_NDI_DEVICE_PROVIDER: Optional[Gst.DeviceProvider] = None


def _pump_glib_for(timeout_s: float, ctx: Optional[GLib.MainContext] = None) -> None:
    """Pump a GLib main context for up to `timeout_s` seconds.

    This implementation avoids running nested GLib.MainLoop instances (which can
    be prone to deadlocks/hangs depending on GI/GLib ownership and signal
    wakeups). Instead, it iterates the context directly.

    NOTE: We must acquire the context while iterating.
    """
    ctx = ctx or GLib.MainContext.new()
    deadline = time.monotonic() + max(0.0, float(timeout_s))

    acquired = False
    try:
        acquired = bool(ctx.acquire())
        # If we can't acquire the context, do a bounded sleep so callers don't
        # hang indefinitely.
        if not acquired:
            time.sleep(min(0.05, max(0.0, float(timeout_s))))
            return

        # Drain/iterate until deadline.
        while True:
            now = time.monotonic()
            if now >= deadline:
                break

            # Process all currently pending events without blocking.
            try:
                while ctx.pending():
                    ctx.iteration(False)
            except Exception:
                # If pending()/iteration() behaves oddly on a platform, keep the
                # loop bounded.
                pass

            # Small sleep to avoid busy-looping; keep latency low.
            time.sleep(0.01)
    finally:
        try:
            if acquired:
                ctx.release()
        except Exception:
            pass


def _get_ndi_device_provider() -> tuple[Optional[GLib.MainContext], Optional[Gst.DeviceProvider]]:
    """Get or create a long-lived ndideviceprovider.

    Starting/stopping ndideviceprovider repeatedly can lead to subsequent
    discovery returning empty. Keeping a single provider running is more
    reliable.
    """
    global _NDI_PROBE_CTX, _NDI_DEVICE_PROVIDER

    if _NDI_PROBE_CTX is not None and _NDI_DEVICE_PROVIDER is not None:
        return (_NDI_PROBE_CTX, _NDI_DEVICE_PROVIDER)

    factory = Gst.DeviceProviderFactory.find("ndideviceprovider")
    if factory is None:
        log.error("probe_failed", reason="ndideviceprovider not available")
        return (None, None)

    ctx = GLib.MainContext.new()
    ctx.push_thread_default()
    try:
        provider = factory.get()
        provider.start()
    except Exception as exc:
        log.warning("probe_error", exc=str(exc))
        try:
            ctx.pop_thread_default()
        except Exception:
            pass
        return (None, None)

    _NDI_PROBE_CTX = ctx
    _NDI_DEVICE_PROVIDER = provider
    return (ctx, provider)


def _shutdown_ndi_device_provider() -> None:
    global _NDI_PROBE_CTX, _NDI_DEVICE_PROVIDER

    provider = _NDI_DEVICE_PROVIDER
    ctx = _NDI_PROBE_CTX

    _NDI_DEVICE_PROVIDER = None
    _NDI_PROBE_CTX = None

    try:
        if provider is not None:
            provider.stop()
    except Exception:
        pass

    try:
        if ctx is not None:
            ctx.pop_thread_default()
    except Exception:
        pass


def probe_ndi_sources(timeout_s: float = 5.0) -> list[str]:
    """Enumerate visible NDI sources via GStreamer's ndideviceprovider."""
    sources: list[str] = []

    ctx, provider = _get_ndi_device_provider()
    if ctx is None or provider is None:
        return sources

    # Pump GLib to allow discovery updates.
    _pump_glib_for(timeout_s, ctx=ctx)

    try:
        for device in provider.get_devices():
            name = device.get_display_name()
            if name:
                sources.append(name)
    except Exception as exc:
        log.warning("probe_error", exc=str(exc))

    return sources


def _discover_demux_src_pad_names() -> tuple[str, str]:
    """
    Inspect the `ndisrcdemux` element factory and return a pair of pad-name
    strings suitable for use in the pipeline string. Tries to find template
    names containing 'video' and 'audio' and falls back to the first two
    src pad templates if those aren't present.
    """
    factory = Gst.ElementFactory.find("ndisrcdemux")
    if not factory:
        return ("demux.video", "demux.audio")

    video_name = None
    audio_name = None
    src_names: list[str] = []
    for tmpl in factory.get_static_pad_templates():
        # Some Gst versions expose direction via method, others via attribute.
        dir_getter = getattr(tmpl, "get_direction", None)
        try:
            if callable(dir_getter):
                direction = dir_getter()
            else:
                direction = getattr(tmpl, "direction", None)
        except Exception:
            direction = None

        if direction is not None and direction != Gst.PadDirection.SRC:
            continue

        # name_template may be like "video" or "src_%u" depending on build
        try:
            name_template = tmpl.get_name_template()
        except Exception:
            try:
                name_template = str(tmpl.get_name())
            except Exception:
                name_template = str(tmpl)
        if "video" in name_template and video_name is None:
            video_name = f"demux.{name_template}"
        if "audio" in name_template and audio_name is None:
            audio_name = f"demux.{name_template}"
        src_names.append(name_template)

    # Fallbacks
    if video_name is None and src_names:
        video_name = f"demux.{src_names[0]}"
    if audio_name is None and len(src_names) > 1:
        audio_name = f"demux.{src_names[1]}"
    if audio_name is None:
        audio_name = "demux.audio"
    if video_name is None:
        video_name = "demux.video"

    return (video_name, audio_name)


# =============================================================================
# BRIDGE CLASS
# =============================================================================

class NdiToWhipBridge:
    """
    Manages the GStreamer pipeline lifecycle, including startup, shutdown,
    error recovery, and reconnect logic.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg          = cfg
        self.pipeline: Optional[Gst.Pipeline] = None
        self.loop: Optional[GLib.MainLoop]    = None
        self._stop_event   = threading.Event()
        self._loop_thread: Optional[threading.Thread] = None
        self._attempt      = 0
        # Continuously updated by the background poller.
        self._visible_sources: set[str]       = set()
        self._sources_lock                    = threading.Lock()
        # Set by the background poller to tell the GLib watchdog (and run())
        # that a better source has appeared and the pipeline should restart.
        self._source_switch_requested = threading.Event()
        # Tracks the source name that ended on failure (not a poller-triggered
        # switch).  Used as a one-cycle exclusion so a dead source that is
        # still cached by the NDI device provider doesn't get re-selected
        # immediately — allowing failover to backup without a round-trip.
        # Reset to None once the exclusion has been applied.
        self._last_failed_source: Optional[str] = None

    # ── Pipeline construction ─────────────────────────────────────────────────

    def _create_pipeline(self) -> Gst.Pipeline:
        # Discover demux pad names for this build of gst-plugins-rs and
        # generate a pipeline string that matches the element's pad templates.
        demux_video_pad, demux_audio_pad = _discover_demux_src_pad_names()
        pipeline_str = build_pipeline_string(self.cfg, demux_video_pad, demux_audio_pad)
        log.debug("pipeline_string", pipeline=pipeline_str)

        try:
            pipeline = Gst.parse_launch(pipeline_str)
        except GLib.Error as exc:
            # If parse_launch fails, attempt a second try with conservative
            # fallback pad names (demux.video/demux.audio) so older plugin
            # variants that use the conventional names still work.
            log.warning("pipeline_parse_failed", error=str(exc), hint="Retrying with fallback demux pad names")
            try:
                pipeline = Gst.parse_launch(build_pipeline_string(self.cfg, "demux.video", "demux.audio"))
            except GLib.Error as exc2:
                raise RuntimeError(
                    f"Failed to parse GStreamer pipeline: {exc2}\n"
                    "Hint: run with --print-pipeline and test manually with gst-launch-1.0"
                ) from exc2

        if not isinstance(pipeline, Gst.Pipeline):
            pipeline = Gst.Pipeline.new("ndi_to_whip")
            # parse_launch returned a bin; wrap it
            pipeline.add(Gst.parse_launch(pipeline_str))

        return pipeline

    # ── Bus message handler ───────────────────────────────────────────────────

    def _on_bus_message(self, bus: Gst.Bus, msg: Gst.Message) -> bool:   # noqa: ARG002
        t = msg.type

        if t == Gst.MessageType.EOS:
            log.warning("pipeline_eos", hint="NDI source ended or WHIP server closed stream")
            self._schedule_reconnect()

        elif t == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log.error(
                "pipeline_error",
                src=msg.src.get_name() if msg.src else "unknown",
                error=err.message,
                debug=debug,
            )
            self._schedule_reconnect()

        elif t == Gst.MessageType.WARNING:
            warn, debug = msg.parse_warning()
            log.warning(
                "pipeline_warning",
                src=msg.src.get_name() if msg.src else "unknown",
                warning=warn.message,
                debug=debug,
            )

        elif t == Gst.MessageType.STATE_CHANGED:
            if msg.src == self.pipeline:
                old, new, pending = msg.parse_state_changed()
                log.info(
                    "state_change",
                    old=old.value_nick,
                    new=new.value_nick,
                    pending=pending.value_nick,
                )

        elif t == Gst.MessageType.ELEMENT:
            structure = msg.get_structure()
            if structure:
                    name = structure.get_name()
                    # WHIP signalling events emitted by whipclientsink
                    if name == "whip-connected":
                        log.info("whip_event", event=name, detail=structure.to_string())
                    elif name == "whip-error":
                        log.error("whip_event", event=name, detail=structure.to_string())
                        # Always attempt to reconnect on WHIP error
                        try:
                            self._schedule_reconnect()
                        except Exception:
                            # Ensure bus handler doesn't raise
                            log.exception("whip_reconnect_failed")
                    elif name == "whip-disconnected":
                        log.info("whip_event", event=name, detail=structure.to_string())
                        # Reconnect on disconnect as well
                        try:
                            self._schedule_reconnect()
                        except Exception:
                            log.exception("whip_reconnect_failed")

        return True  # keep watching

    # ── Reconnect logic ───────────────────────────────────────────────────────

    def _schedule_reconnect(self) -> None:
        """Stop current pipeline and signal the run loop to reconnect."""
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self.loop and self.loop.is_running():
            self.loop.quit()

    def _reconnect_delay(self) -> float:
        delay = min(
            self.cfg.retry_initial_delay_s
            * (self.cfg.retry_backoff_factor ** max(0, self._attempt - 1)),
            self.cfg.retry_max_delay_s,
        )
        return delay

    # ── GLib loop in background thread ────────────────────────────────────────

    def _run_glib_loop(self) -> None:
        self.loop = GLib.MainLoop()

        # Capture these here so the closure doesn't hold a strong ref to self
        # (avoids keeping the bridge alive beyond its useful life).
        stop_evt   = self._stop_event
        switch_evt = self._source_switch_requested
        loop_ref   = self.loop

        def _watchdog() -> bool:
            """Called every 500 ms in the GLib thread.

            Quits the main loop if a source-switch or global stop was
            requested by another thread.  Doing the quit here (inside the
            GLib thread) avoids the race where the poller thread called
            loop.quit() before self.loop was even assigned.
            """
            if stop_evt.is_set() or switch_evt.is_set():
                if loop_ref.is_running():
                    loop_ref.quit()
                return False   # remove this timer source
            return True        # keep firing

        GLib.timeout_add(500, _watchdog)
        self.loop.run()

    def _try_establish_source(self, src: str, attempt_timeout_s: float = 5.0) -> bool:
        """
        Try to create and start a temporary pipeline for `src` to verify that a
        connection to that NDI source can be established. Returns True if the
        temporary pipeline reached PLAYING within `attempt_timeout_s` seconds.
        This does not modify the bridge's active pipeline/state.
        """
        # Preserve current cfg values
        old_source = self.cfg.ndi_source_name
        old_timeout = self.cfg.ndi_connect_timeout_ms
        try:
            self.cfg.ndi_source_name = src
            self.cfg.ndi_connect_timeout_ms = int(attempt_timeout_s * 1000)
            try:
                pipeline = self._create_pipeline()
            except Exception:
                return False

            # Try to start the temporary pipeline and wait for state
            ret = pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                pipeline.set_state(Gst.State.NULL)
                return False

            # Wait up to attempt_timeout_s for the pipeline to reach PLAYING
            # get_state expects microseconds
            try:
                state_change = pipeline.get_state(int(attempt_timeout_s * Gst.SECOND))
            except Exception:
                state_change = (None, None, None)

            # state_change may be (return, pending, state)
            is_playing = False
            try:
                if len(state_change) >= 3 and state_change[2] == Gst.State.PLAYING:
                    is_playing = True
            except Exception:
                is_playing = False

            pipeline.set_state(Gst.State.NULL)
            return is_playing
        finally:
            self.cfg.ndi_source_name = old_source
            self.cfg.ndi_connect_timeout_ms = old_timeout

    def _start_background_poller(
        self, primary: str, backup: Optional[str] = None, poll_interval_s: float = 10.0
    ) -> tuple[threading.Thread, threading.Event]:
        """
        Start a persistent background thread that runs for the full bridge
        lifetime.  Every ``poll_interval_s`` seconds it probes the network,
        updates ``_visible_sources``, and fires ``_source_switch_requested``
        (which the GLib watchdog converts into a clean pipeline quit) for two
        transitions:

        * backup  → primary : primary reappears while we are streaming backup.
        * primary → backup  : primary disappears while we are streaming primary
                              AND backup is configured.  This handles the case
                              where ndisrc retries silently instead of emitting
                              a GStreamer error, keeping ``_run_once()`` alive
                              indefinitely.

        Returns ``(thread, poller_stop_event)``; call
        ``poller_stop_event.set()`` to shut it down cleanly.
        """
        stop_evt    = self._stop_event
        poller_stop = threading.Event()

        def _poller() -> None:
            log.info(
                "background_poller_started",
                primary=primary, backup=backup, interval_s=poll_interval_s,
            )
            while not stop_evt.is_set() and not poller_stop.is_set():
                try:
                    sources = probe_ndi_sources(timeout_s=5.0)
                    with self._sources_lock:
                        self._visible_sources = set(sources)
                    log.debug("sources_updated", visible=sorted(sources))

                    current = self.cfg.ndi_source_name

                    if not self._source_switch_requested.is_set():
                        # backup → primary: primary came back while on backup.
                        if primary in sources and current != primary:
                            log.info("primary_appeared_switching", primary=primary)
                            self.cfg.ndi_source_name = primary
                            self._source_switch_requested.set()

                        # primary → backup: primary gone while on primary.
                        # ndisrc may retry silently (no GStreamer error) so the
                        # pipeline would hang indefinitely without this check.
                        elif (
                            primary not in sources
                            and current == primary
                            and backup is not None
                        ):
                            log.info(
                                "primary_gone_switching_to_backup",
                                primary=primary, backup=backup,
                            )
                            # Do NOT set cfg.ndi_source_name here — let run()
                            # re-evaluate via _visible_sources in the next cycle
                            # (primary will be absent, so backup is picked).
                            self._source_switch_requested.set()

                except Exception as exc:
                    log.warning("background_poller_error", exc=str(exc))

                # Interruptible sleep — woken immediately by poller_stop.set()
                poller_stop.wait(timeout=poll_interval_s)

            log.info("background_poller_stopped")

        t = threading.Thread(target=_poller, daemon=True)
        t.start()
        return t, poller_stop

    # ── Single pipeline run ───────────────────────────────────────────────────

    def _run_once(self) -> None:
        """Start a single pipeline run; returns when the pipeline ends/errors."""
        self._attempt += 1
        log.info("pipeline_start", attempt=self._attempt, ndi=self.cfg.ndi_source_name)

        self.pipeline = self._create_pipeline()

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        # Install simple pad probes to measure A/V timestamp offset for
        # runtime diagnostics. Logs `av_sync_offset_ms` when both streams
        # produce buffers.
        def _maybe_log_av_offset():
            try:
                v = getattr(self, "_last_video_pts", None)
                a = getattr(self, "_last_audio_pts", None)
                if v is None or a is None:
                    return
                # Convert nanoseconds to milliseconds
                offset_ms = (int(a) - int(v)) / (Gst.SECOND / 1000)
                # record sample for moving-median
                try:
                    self._av_samples.append(offset_ms)
                except Exception:
                    pass
                log.info("av_sync_offset_ms", offset_ms=round(offset_ms, 1))
            except Exception:
                pass

        def _make_probe(attr_name):
            def _probe(pad, info):
                buf = info.get_buffer()
                if buf is None:
                    return Gst.PadProbeReturn.OK
                pts = buf.pts
                if pts == Gst.CLOCK_TIME_NONE:
                    return Gst.PadProbeReturn.OK
                setattr(self, attr_name, pts)
                _maybe_log_av_offset()
                return Gst.PadProbeReturn.OK
            return _probe

        try:
            vqueue = self.pipeline.get_by_name("vqueue")
            if vqueue is not None:
                vpad = vqueue.get_static_pad("src")
                if vpad:
                    vpad.add_probe(Gst.PadProbeType.BUFFER, _make_probe("_last_video_pts"))
            aqueue = self.pipeline.get_by_name("aqueue")
            if aqueue is not None:
                apad = aqueue.get_static_pad("src")
                if apad:
                    apad.add_probe(Gst.PadProbeType.BUFFER, _make_probe("_last_audio_pts"))
        except Exception:
            log.debug("av_probe_setup_failed")

        # Moving-median based audio-delay adjuster (runs in GLib mainloop)
        try:
            self._av_samples = deque(maxlen=60)

            def _adjust_delay():
                try:
                    if not hasattr(self, "_av_samples") or not self._av_samples:
                        return True
                    samples = list(self._av_samples)
                    med = statistics.median(samples)
                    # If median < -2 ms then audio leads video: delay audio by -med
                    if med < -2.0:
                        desired_ms = int(min(max(0, -med), 2000))
                    else:
                        desired_ms = 0
                    adel = self.pipeline.get_by_name("adel") if self.pipeline else None
                    if adel is not None:
                        try:
                            # `adelay.delay` expects milliseconds
                            adel.set_property("delay", desired_ms)
                            log.debug("adelay_set", delay_ms=desired_ms)
                        except Exception:
                            log.debug("adelay_set_failed")
                except Exception:
                    pass
                return True

            # run every 500ms
            GLib.timeout_add(500, _adjust_delay)
        except Exception:
            log.debug("av_delay_adjuster_failed")

        # Start GLib loop in background thread
        self._loop_thread = threading.Thread(target=self._run_glib_loop, daemon=True)
        self._loop_thread.start()

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            log.error("pipeline_start_failed", hint="Check NDI source name and WHIP URL")
            self._schedule_reconnect()

        # Wait for loop to exit (due to error, EOS, or stop signal)
        self._loop_thread.join()
        log.info("pipeline_stopped", attempt=self._attempt)

        # Cleanup
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    # ── Main public API ───────────────────────────────────────────────────────

    def run(self) -> None:
        """Run the bridge with retry/reconnect until stopped.

        A single background poller runs for the full bridge lifetime, keeping
        ``_visible_sources`` up to date every 10 s.  The main loop picks the
        best available source each cycle:

        * Primary visible → stream primary.
        * Primary not visible, backup configured → stream backup.
        * Neither visible → exponential backoff, retry.

        When the poller detects that primary has reappeared while we are
        streaming backup, it sets ``cfg.ndi_source_name = primary`` and
        ``_source_switch_requested``.  The GLib watchdog (in
        ``_run_glib_loop``) detects this flag within 500 ms and quits the
        pipeline cleanly from within the GLib thread.  The main loop then
        skips backoff and immediately retries, streaming primary.
        """
        log.info(
            "bridge_starting",
            ndi_source=self.cfg.ndi_source_name,
            whip_url=self.cfg.whip_url,
            encoder=self.cfg.video_encoder,
            resolution=f"{self.cfg.video_width}x{self.cfg.video_height}@{self.cfg.video_framerate}",
            video_bitrate_kbps=self.cfg.video_bitrate_kbps,
            audio_bitrate_bps=self.cfg.audio_bitrate_bps,
        )

        primary      = self.cfg.ndi_source_name
        backup       = (
            self.cfg.backup_ndi_source_name
            if self.cfg.backup_ndi_source_name
               and self.cfg.backup_ndi_source_name != primary
            else None
        )
        max_attempts = self.cfg.retry_max_attempts

        log.info("ndi_sources", primary=primary, backup=backup)

        # Start the persistent background poller once for the full lifetime of
        # the bridge.  It updates _visible_sources every 10 s regardless of
        # which source is active, and signals switches in both directions:
        # backup→primary (primary reappears) and primary→backup (primary gone).
        poll_thread, poller_stop = self._start_background_poller(
            primary, backup=backup, poll_interval_s=10.0
        )

        try:
            while not self._stop_event.is_set():
                # Clear stale switch flag at the top of every cycle.
                self._source_switch_requested.clear()

                # Use the latest source list from the background poller.
                # On the very first cycle it may be empty — seed it with a
                # quick synchronous probe so we don't blindly try backup.
                with self._sources_lock:
                    visible = set(self._visible_sources)

                if not visible:
                    try:
                        initial = probe_ndi_sources(timeout_s=2.0)
                    except Exception:
                        initial = []
                    with self._sources_lock:
                        self._visible_sources = set(initial)
                    visible = set(initial)

                # Pick the best source: primary if visible AND it didn't just
                # fail this cycle (NDI device provider caches dead sources for
                # up to ~30 s, so we must exclude them explicitly).
                primary_excluded = (self._last_failed_source == primary)
                self._last_failed_source = None  # consume the one-cycle exclusion

                if primary in visible and not primary_excluded:
                    self.cfg.ndi_source_name = primary
                    log.info("source_selected", source=primary, reason="primary_visible")
                elif backup:
                    self.cfg.ndi_source_name = backup
                    if primary_excluded:
                        reason = "primary_just_failed_using_backup"
                    elif backup in visible:
                        reason = "primary_absent_backup_visible"
                    else:
                        reason = "primary_absent_trying_backup"
                    log.info("source_selected", source=backup, reason=reason)
                else:
                    self.cfg.ndi_source_name = primary
                    log.info("source_selected", source=primary, reason="no_backup_configured")

                # Remember which source we are about to attempt so we can
                # record a failure against it after _run_once() returns.
                attempted_source = self.cfg.ndi_source_name

                # Run the pipeline for the chosen source.
                try:
                    self._run_once()
                except Exception as exc:
                    log.exception("pipeline_exception", exc=str(exc))

                if self._stop_event.is_set():
                    break

                if max_attempts and self._attempt >= max_attempts:
                    log.error("retry_limit_reached", attempts=self._attempt)
                    self._stop_event.set()
                    break

                # If the poller triggered a switch, don't record a failure —
                # the pipeline ended intentionally.  Skip backoff immediately.
                if self._source_switch_requested.is_set():
                    log.info("immediate_retry", next_source=self.cfg.ndi_source_name)
                    continue

                # Pipeline ended unexpectedly.  Record the failure so the next
                # cycle excludes this source and tries the other one.
                self._last_failed_source = attempted_source
                log.info("source_failed", source=attempted_source)

                # If the source that just failed is primary and backup is
                # configured, switch to backup immediately without backoff.
                if attempted_source == primary and backup:
                    log.info("failover_to_backup", backup=backup)
                    continue

                # Ordinary failure (backup died, or no backup) — backoff.
                delay = self._reconnect_delay()
                log.info("reconnect_waiting", delay_s=round(delay, 1), attempt=self._attempt)
                self._stop_event.wait(timeout=delay)

        finally:
            poller_stop.set()
            try:
                poll_thread.join(timeout=2.0)
            except Exception:
                pass

        log.info("bridge_stopped")

    def stop(self) -> None:
        """Signal a clean shutdown."""
        log.info("stop_requested")
        self._stop_event.set()
        self._schedule_reconnect()


# =============================================================================
# CLI / ENTRY POINT
# =============================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="NDI → WHIP bridge (GStreamer-based)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--config",          metavar="PATH",
                   help="Path to TOML config file")
    p.add_argument("--probe",           action="store_true",
                   help="Discover and print visible NDI sources, then exit")
    p.add_argument("--probe-timeout",   type=float, default=5.0, metavar="S",
                   help="Seconds to wait while probing for NDI sources")
    p.add_argument("--print-pipeline",  action="store_true",
                   help="Print the generated GStreamer pipeline string, then exit")
    p.add_argument("--validate",        action="store_true",
                   help="Check that all required GStreamer elements exist, then exit")

    # Overrides (all optional — TOML file takes precedence if not given)
    g = p.add_argument_group("overrides (take precedence over config file)")
    g.add_argument("--ndi-source",      metavar="NAME",  dest="ndi_source_name")
    g.add_argument("--backup-ndi-source", metavar="NAME", dest="backup_ndi_source_name",
                   help="Optional backup NDI source name if primary is unavailable")
    g.add_argument("--whip-url",        metavar="URL",   dest="whip_url")
    g.add_argument("--auth-token",      metavar="TOKEN", dest="auth_token")
    g.add_argument("--width",           type=int,        dest="video_width")
    g.add_argument("--height",          type=int,        dest="video_height")
    g.add_argument("--framerate",       type=int,        dest="video_framerate")
    g.add_argument("--video-bitrate",   type=int,        dest="video_bitrate_kbps",
                   metavar="KBPS")
    g.add_argument("--encoder",         choices=list(ENCODER_PROFILES),
                   dest="video_encoder")
    g.add_argument("--audio-bitrate",   type=int,        dest="audio_bitrate_bps",
                   metavar="BPS")
    g.add_argument("--log-level",       dest="log_level",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def validate_elements() -> bool:
    required = [
        "ndisrc", "ndisrcdemux",
        "whipclientsink",
        "videoconvert", "videoscale", "videorate",
        "x264enc", "opusenc",
        "rtph264pay", "rtpopuspay",
        "audioconvert", "audioresample",
        "identity", "queue", "adelay",
    ]
    all_ok = True
    for name in required:
        factory = Gst.ElementFactory.find(name)
        status  = "OK" if factory else "MISSING"
        if not factory:
            all_ok = False
        print(f"  {'✓' if factory else '✗'}  {name:25s}  {status}")
    return all_ok


def main() -> None:
    args = parse_args()

    # Build overrides dict (only non-None values)
    overrides = {k: v for k, v in vars(args).items()
                 if k not in {"config", "probe", "probe_timeout",
                              "print_pipeline", "validate"}
                 and v is not None}

    cfg = load_config(args.config, overrides)

    # Configure GStreamer debug level
    os.environ.setdefault("GST_DEBUG", cfg.log_gst_debug)
    os.environ.setdefault("GST_PLUGIN_PATH",
                          "/usr/local/lib/gstreamer-1.0")
    os.environ.setdefault("LD_LIBRARY_PATH",
                          "/usr/local/lib:" + os.environ.get("LD_LIBRARY_PATH", ""))

    # Initialize GStreamer after environment variables are set so custom
    # plugins (gst-plugins-rs builds) in GST_PLUGIN_PATH are discoverable.
    try:
        Gst.init(None)
    except Exception as exc:
        sys.exit(f"[FATAL] Failed to initialize GStreamer: {exc}")

    # Configure Python log level
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(level)

    # ── Modes ─────────────────────────────────────────────────────────────────

    if args.validate:
        print("Validating required GStreamer elements:")
        ok = validate_elements()
        sys.exit(0 if ok else 1)

    if args.probe:
        print(f"Probing for NDI sources ({args.probe_timeout}s)…")
        sources = probe_ndi_sources(timeout_s=args.probe_timeout)
        log.debug("probe_results", sources=sources)
        if sources:
            print(f"\nFound {len(sources)} NDI source(s):")
            for s in sources:
                print(f"  • {s}")
        else:
            print("  No NDI sources found. Check network and NDI sender.")
        # Cleanly stop provider when running in one-shot probe mode.
        _shutdown_ndi_device_provider()
        sys.exit(0)

    if args.print_pipeline:
        print(build_pipeline_string(cfg))
        sys.exit(0)

    # ── Streaming mode ───────────────────────────────────────────────────────

    bridge = NdiToWhipBridge(cfg)

    def _sig_handler(signum: int, _frame) -> None:  # type: ignore[type-arg]
        sig_name = signal.Signals(signum).name
        log.info("signal_received", signal=sig_name)
        bridge.stop()

    signal.signal(signal.SIGINT,  _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    try:
        bridge.run()
    finally:
        _shutdown_ndi_device_provider()


if __name__ == "__main__":
    main()
