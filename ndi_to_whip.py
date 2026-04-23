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
            max-size-time=0
            max-size-bytes=0
        ! videoconvert
        ! videoscale
        ! videorate
        ! {video_caps}
        ! whip.

        {demux_audio_pad}
        ! queue name=aqueue
            leaky=downstream
            max-size-buffers=10
            max-size-time=0
            max-size-bytes=0
        ! audioconvert
        ! audioresample
        ! {audio_caps}
        ! whip.
    """

    # Collapse extra whitespace for clean logging
    return " ".join(pipeline.split())


# =============================================================================
# NDI SOURCE PROBE
# =============================================================================


def _pump_glib_for(timeout_s: float, ctx: Optional[GLib.MainContext] = None) -> None:
    """Pump a GLib main context for `timeout_s` seconds.

    Some GStreamer device providers (including ndideviceprovider) rely on GLib
    main-context dispatch to deliver discovery events. Using time.sleep() does
    not dispatch those events and can lead to intermittent/empty discovery.

    If `ctx` is provided, we pump that context; otherwise we create a private
    context.
    """
    timeout_ms = max(0, int(timeout_s * 1000))

    # Use a private context unless caller supplies one.
    ctx = ctx or GLib.MainContext.new()
    loop = GLib.MainLoop.new(ctx, False)

    def _quit() -> bool:
        try:
            loop.quit()
        except Exception:
            pass
        return False  # do not repeat

    GLib.timeout_add(timeout_ms, _quit, context=ctx)

    try:
        loop.run()
    except Exception:
        # If the loop fails for any reason, just return; probe will still try to
        # read whatever devices are available.
        return


def probe_ndi_sources(timeout_s: float = 5.0) -> list[str]:
    """Enumerate visible NDI sources via GStreamer's ndideviceprovider.

    Returns a list of source display-name strings.

    NOTE: We intentionally pump a GLib main context for `timeout_s` rather than
    using time.sleep(). On Linux with Avahi, this dramatically improves
    reliability when called from a background thread.

    IMPORTANT: The device provider may post discovery to the *thread-default*
    GLib main context. We therefore push a private context as thread-default
    for the duration of the probe.
    """
    sources: list[str] = []

    try:
        factory = Gst.DeviceProviderFactory.find("ndideviceprovider")
        if factory is None:
            log.error("probe_failed", reason="ndideviceprovider not available")
            return sources

        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        try:
            provider = factory.get()
            provider.start()

            _pump_glib_for(timeout_s, ctx=ctx)

            for device in provider.get_devices():
                name = device.get_display_name()
                if name:
                    sources.append(name)

            provider.stop()
        finally:
            try:
                ctx.pop_thread_default()
            except Exception:
                pass

    except Exception as exc:
        log.warning("probe_error", exc=str(exc))

    return sources


# =============================================================================
# (rest of file unchanged)
# =============================================================================
