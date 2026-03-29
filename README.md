# NDI → WHIP Bridge

Production-grade Ubuntu service that captures a named NDI source and publishes it
to a WHIP (WebRTC HTTP Ingest Protocol) endpoint as H.264 + Opus over WebRTC.

Built on GStreamer with `whipclientsink` from `gst-plugins-rs` (Rust). Single
pipeline, no intermediate hops, low latency. Tested on Ubuntu 24.04 with
GStreamer 1.24 and gst-plugins-rs `gstreamer-1.24.13`.

---

## Contents

- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
  - [1. System packages](#1-system-packages)
  - [2. Rust toolchain](#2-rust-toolchain)
  - [3. NDI SDK (manual)](#3-ndi-sdk-manual)
  - [4. Build GStreamer plugins](#4-build-gstreamer-plugins)
  - [5. Application setup](#5-application-setup)
- [MediaMTX — local WHIP server](#mediamtx--local-whip-server)
- [Configuration](#configuration)
- [Running](#running)
  - [Manual / foreground](#manual--foreground)
  - [As a systemd service](#as-a-systemd-service)
- [Viewing the stream](#viewing-the-stream)
- [Testing and Validation](#testing-and-validation)
- [Troubleshooting](#troubleshooting)
- [Hardware Encoding](#hardware-encoding)
- [Known Limitations](#known-limitations)

---

## Architecture

```
NDI network
    │
    ▼
ndisrc ──► ndisrcdemux
               │
               ├── video (raw) ──► videoconvert ► videoscale ► videorate
               │                   video/x-raw,I420,1920x1080,30fps
               │                                               ┐
               └── audio (raw) ──► audioconvert ► audioresample├──► whipclientsink
                                   audio/x-raw,S16LE,48kHz,2ch ┘    (GstBaseWebRTCSink)
                                                                      │
                                                               H.264 + Opus
                                                               WHIP HTTP POST
                                                               ICE + DTLS-SRTP
                                                                      │
                                                            WHIP ingest endpoint
```

**Key design notes (gst-plugins-rs ≥ 1.24):**

- `whipclientsink` is `GstBaseWebRTCSink` — it accepts **raw** video/audio and
  handles codec negotiation and encoding internally. Do not pre-encode.
- `video-caps="video/x-h264"` constrains codec selection to H.264, which is
  required for compatibility with Safari and most WHIP servers.
- `ndisrcdemux` (not `ndidemux`) is the correct pad demuxer name in 1.24.x.
- `signaller::whip-endpoint` (GstChildProxy child property) replaces the old
  `sig-server-url` property removed in 1.24.

---

## Requirements

| Component | Minimum version | Notes |
|---|---|---|
| Ubuntu | 22.04 LTS | 24.04 preferred (ships GStreamer 1.24) |
| GStreamer | 1.20 | 1.24 recommended; must match gst-plugins-rs tag |
| Rust / Cargo | stable | Build-time only |
| NDI SDK | v5 or v6 | Proprietary — download from ndi.tv |
| Python | 3.10 | 3.11+ gets `tomllib` without extra install |
| libnice | any | Ships with Ubuntu — ICE for WebRTC |
| libsrtp2 | any | Ships with Ubuntu — SRTP |

---

## Installation

> Run all steps on the target Ubuntu server. The automated installer handles
> everything below — run it if you prefer:
>
> ```bash
> sudo bash install.sh
> ```
>
> The NDI SDK must be installed manually first (step 3).

### 1. System packages

```bash
sudo apt update
sudo apt install -y \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  gstreamer1.0-nice \
  libgstreamer1.0-dev \
  libgstreamer-plugins-base1.0-dev \
  libgstreamer-plugins-bad1.0-dev \
  libnice-dev \
  libssl-dev \
  libsrtp2-dev \
  libx264-dev \
  libopus-dev \
  build-essential pkg-config cmake ninja-build meson \
  git curl wget ca-certificates nasm \
  python3 python3-pip python3-venv python3-gi \
  python3-gi-cairo \
  gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0
```

**Ubuntu 22.04 note:** Ships GStreamer 1.20. Upgrade to 24.04 for GStreamer 1.24,
which is the recommended target for this project.

---

### 2. Rust toolchain

Rust is required to build `gst-plugins-rs`. Only needed at build time.

```bash
export CARGO_HOME=/opt/cargo
export RUSTUP_HOME=/opt/rustup
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
  | sh -s -- -y --no-modify-path --profile minimal
export PATH="/opt/cargo/bin:$PATH"

cargo --version   # verify
```

---

### 3. NDI SDK (manual)

The NDI SDK is proprietary and requires accepting a licence agreement.

1. Go to **https://www.ndi.tv/sdk/** and create a free account
2. Download the Linux installer (`Install_NDI_SDK_v6_Linux.tar.gz` or similar)
3. Extract and run:

```bash
tar -xzf Install_NDI_SDK_v6_Linux.tar.gz
sudo bash Install_NDI_SDK_v6_Linux.sh
# Accept the EULA, install to /usr/local (the default)
sudo ldconfig
```

4. Verify:

```bash
ls /usr/local/include/Processing.NDI.Lib.h   # must exist
ls /usr/local/lib/libndi.so*                 # must exist
```

---

### 4. Build GStreamer plugins

Builds `libgstndi.so` (NDI source) and `libgstrswebrtc.so` (WHIP client) from
the `gst-plugins-rs` Rust workspace. The tag must match your system GStreamer version.

```bash
git clone --branch gstreamer-1.24.13 --depth 1 \
  https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git
cd gst-plugins-rs

# Build both plugins (5–15 minutes on first run)
NDI_SDK_DIR=/usr/local cargo build --release \
  --package gst-plugin-ndi \
  --package gst-plugin-webrtc

# Install to the system GStreamer plugin directory
sudo mkdir -p /usr/local/lib/gstreamer-1.0
sudo install -m 755 target/release/libgstndi.so      /usr/local/lib/gstreamer-1.0/
sudo install -m 755 target/release/libgstrswebrtc.so /usr/local/lib/gstreamer-1.0/
sudo ldconfig
```

> **Important:** The file is `libgstrswebrtc.so` (with `rs`), not `libgstwebrtc.so`.
> The Ubuntu package `gstreamer1.0-plugins-bad` ships `libgstwebrtc.so` which provides
> `webrtcbin` but **not** `whipclientsink`. Always set:
> ```bash
> export GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0
> ```

**Verify both elements load:**

```bash
export GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0
gst-inspect-1.0 ndisrc          # must print element details
gst-inspect-1.0 ndisrcdemux     # must print element details
gst-inspect-1.0 whipclientsink  # must print GstWhipWebRTCSink details
```

---

### 5. Application setup

```bash
# Create service user
sudo useradd --system --no-create-home --shell /usr/sbin/nologin ndi-whip
sudo usermod -aG video,audio ndi-whip

# Create directories
sudo mkdir -p /opt/ndi_to_whip /etc/ndi_to_whip /var/log/ndi_to_whip
sudo mkdir -p /var/cache/gstreamer-1.0

# Copy application files
sudo cp ndi_to_whip.py /opt/ndi_to_whip/
sudo cp config.toml    /etc/ndi_to_whip/
sudo chmod +x /opt/ndi_to_whip/ndi_to_whip.py
sudo chmod 640 /etc/ndi_to_whip/config.toml

# Python virtual environment (--system-site-packages for PyGObject access)
sudo python3 -m venv --system-site-packages /opt/ndi_to_whip/venv
sudo /opt/ndi_to_whip/venv/bin/pip install --upgrade pip
sudo /opt/ndi_to_whip/venv/bin/pip install tomli structlog

# GStreamer registry cache (writable by service user under ProtectSystem=strict)
sudo chown ndi-whip:ndi-whip /var/cache/gstreamer-1.0

# Fix ownership
sudo chown -R ndi-whip:ndi-whip /opt/ndi_to_whip /var/log/ndi_to_whip

# Persist NDI library path
echo '/usr/local/lib' | sudo tee /etc/ld.so.conf.d/ndi.conf
sudo ldconfig

# Persist environment for all shells
sudo tee /etc/profile.d/ndi_to_whip.sh <<'EOF'
export GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0
export LD_LIBRARY_PATH=/usr/local/lib
export NDI_SDK_DIR=/usr/local
EOF

# Install systemd service
sudo cp ndi-to-whip.service /etc/systemd/system/
sudo systemctl daemon-reload
```

---

## MediaMTX — local WHIP server

[MediaMTX](https://github.com/bluenviron/mediamtx) is a lightweight media server
that acts as a WHIP ingest endpoint and re-publishes the stream over WebRTC, HLS,
RTSP, and RTMP.

### Install MediaMTX

```bash
# Download latest release (check https://github.com/bluenviron/mediamtx/releases)
wget https://github.com/bluenviron/mediamtx/releases/download/v1.12.2/mediamtx_v1.12.2_linux_amd64.tar.gz
tar -xzf mediamtx_v1.12.2_linux_amd64.tar.gz
sudo mv mediamtx /usr/local/bin/

# Create config directory and install config
sudo mkdir -p /etc/mediamtx
sudo cp mediamtx.yml /etc/mediamtx/mediamtx.yml
```

### Configure MediaMTX

The included `mediamtx.yml` is pre-configured for this project:

```yaml
webrtcAddress: :8889

paths:
  live: {}
```

### Run as a systemd service

Create `/etc/systemd/system/mediamtx.service`:

```ini
[Unit]
Description=MediaMTX
After=network.target

[Service]
ExecStart=/usr/local/bin/mediamtx /etc/mediamtx/mediamtx.yml
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx
```

### WHIP endpoint for config.toml

```toml
[whip]
url = "http://127.0.0.1:8889/live/whip"
```

---

## Configuration

Edit `/etc/ndi_to_whip/config.toml` before starting the service.
Restart after any change: `sudo systemctl restart ndi-to-whip`

```toml
[ndi]
source_name = "MYCOMPUTER (Stream 1)"   # exact NDI source name

[whip]
url         = "http://127.0.0.1:8889/live/whip"
auth_token  = ""                        # Bearer token, leave "" if not required
stun_server = "stun://stun.l.google.com:19302"

[video]
width        = 1920
height       = 1080
framerate    = 30
bitrate_kbps = 4000
encoder      = "x264"    # x264 | vaapi | nvenc

[audio]
channels    = 2
sample_rate = 48000
bitrate_bps = 128000
```

### WHIP endpoint examples

| Service | URL format |
|---|---|
| MediaMTX (local) | `http://127.0.0.1:8889/live/whip` |
| Cloudflare Stream | `https://customer-<id>.cloudflarestream.com/<stream-key>/webRTC/publish` |
| LiveKit | `https://your-livekit-host/whip` |
| Janus (local) | `http://127.0.0.1:8088/janus/whip` |

---

## Running

### Manual / foreground

```bash
# Required environment (set automatically by the systemd unit)
export GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0
export LD_LIBRARY_PATH=/usr/local/lib

# Discover NDI sources on the network
/opt/ndi_to_whip/venv/bin/python /opt/ndi_to_whip/ndi_to_whip.py --probe

# Print the generated GStreamer pipeline without running it
/opt/ndi_to_whip/venv/bin/python /opt/ndi_to_whip/ndi_to_whip.py \
  --config /etc/ndi_to_whip/config.toml --print-pipeline

# Run interactively
/opt/ndi_to_whip/venv/bin/python /opt/ndi_to_whip/ndi_to_whip.py \
  --config /etc/ndi_to_whip/config.toml

# Run with inline overrides (no config file needed)
/opt/ndi_to_whip/venv/bin/python /opt/ndi_to_whip/ndi_to_whip.py \
  --ndi-source "MYPC (Camera 1)" \
  --whip-url "http://127.0.0.1:8889/live/whip" \
  --width 1280 --height 720 --framerate 30 \
  --log-level DEBUG
```

Stop with `Ctrl+C` — triggers a clean SIGINT shutdown.

### As a systemd service

```bash
# Enable and start (also starts on every boot)
sudo systemctl enable --now ndi-to-whip

# Check status
sudo systemctl status ndi-to-whip

# Follow live logs
sudo journalctl -u ndi-to-whip -f

# Restart after config changes
sudo systemctl restart ndi-to-whip

# Stop
sudo systemctl stop ndi-to-whip

# View last 200 log lines
sudo journalctl -u ndi-to-whip -n 200 --no-pager
```

---

## Viewing the stream

When using MediaMTX as the WHIP server, the stream is available on multiple protocols.

### WebRTC (lowest latency, ~0.5s)

Open in a browser:
```
http://<server-ip>:8889/live/
```

> **Note:** WebRTC playback requires ICE to succeed. If the browser and server are
> on different subnets or behind NAT, ICE may fail. Add `webrtcLocalUDPAddress`
> to `/etc/mediamtx/mediamtx.yml` to help:
> ```yaml
> webrtcLocalUDPAddress: <server-ip>:8189
> ```

### HLS (~5–10s latency)

Open in a browser or media player:
```
http://<server-ip>:8888/live/
```

Direct playlist URL:
```
http://<server-ip>:8888/live/index.m3u8
```

> **Safari / iOS note:** Safari does not support Opus audio in HLS. The HLS stream
> will show a broken play button in Safari. Use VLC (see below) or the WebRTC
> player instead.

### VLC (recommended for iOS / Android)

1. Install **VLC** from the App Store or Google Play
2. Tap the network/stream icon → **Open Network Stream**
3. Enter:
   ```
   http://<server-ip>:8888/live/index.m3u8
   ```

VLC supports Opus audio and plays the HLS stream reliably on all platforms.

### RTSP

```
rtsp://<server-ip>:8554/live
```

---

## Testing and Validation

### Pre-flight checklist

```bash
# Runs ~20 automated checks and prints pass/warn/fail
sudo bash validate.sh

# Skip the 5-second NDI network scan
bash validate.sh --quick
```

### Validate GStreamer elements only

```bash
GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0 \
/opt/ndi_to_whip/venv/bin/python /opt/ndi_to_whip/ndi_to_whip.py --validate
```

### Test the pipeline manually with gst-launch-1.0

```bash
GST_DEBUG=whipclientsink:4 \
GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0 \
LD_LIBRARY_PATH=/usr/local/lib \
gst-launch-1.0 \
  whipclientsink name=whip \
    signaller::whip-endpoint="http://127.0.0.1:8889/live/whip" \
    video-caps="video/x-h264" \
  ndisrc ndi-name="SOURCENAME" ! ndisrcdemux name=demux \
  demux.video ! queue leaky=downstream ! videoconvert ! videoscale ! videorate ! \
    "video/x-raw,format=I420,width=1280,height=720,framerate=30/1" ! whip. \
  demux.audio ! queue leaky=downstream ! audioconvert ! audioresample ! \
    "audio/x-raw,rate=48000,channels=2,format=S16LE,layout=interleaved" ! whip.
```

### Test with a video test source (no NDI required)

```bash
GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0 \
gst-launch-1.0 \
  whipclientsink name=whip \
    signaller::whip-endpoint="http://127.0.0.1:8889/live/whip" \
    video-caps="video/x-h264" \
  videotestsrc ! video/x-raw,width=1280,height=720,framerate=30/1 ! whip. \
  audiotestsrc ! audio/x-raw,rate=48000,channels=2 ! whip.
```

### Verify HLS segments are being served

```bash
curl -s http://127.0.0.1:8888/live/index.m3u8
```

---

## Troubleshooting

### NDI source not found / `--probe` returns nothing

NDI uses mDNS (UDP/5353) for discovery and UDP/5960 for video. Both must be open.

```bash
sudo ufw allow 5353/udp
sudo ufw allow 5960

# Verify multicast route
ip route show | grep '224.0.0.0'
# If missing:
sudo ip route add 224.0.0.0/4 dev eth0
```

NDI discovery fails across VLANs unless multicast is routed. If your NDI sender
is on a different VLAN, specify its IP directly in `source_name`:
```toml
source_name = "192.168.1.50 (Stream Name)"
```

---

### `ndisrc` or `ndisrcdemux` element not found

```bash
# Confirm the plugin file exists
ls -lh /usr/local/lib/gstreamer-1.0/libgstndi.so

# Check for missing shared library dependencies
ldd /usr/local/lib/gstreamer-1.0/libgstndi.so | grep "not found"
# If libndi.so is missing:
sudo ldconfig
```

---

### `whipclientsink` not found

```bash
# Confirm the file exists (note: libgstrswebrtc.so, not libgstwebrtc.so)
ls /usr/local/lib/gstreamer-1.0/libgstrswebrtc.so

# Verify with GST_PLUGIN_PATH set
GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0 gst-inspect-1.0 whipclientsink
```

If `gst-inspect-1.0 whipclientsink` shows `webrtcbin` info instead of
`GstWhipWebRTCSink`, the Ubuntu system plugin is loading instead of the
`gst-plugins-rs` build. Ensure `GST_PLUGIN_PATH` is set correctly.

---

### `No codec available for encoding stream video_0`

`GstBaseWebRTCSink` scans for encoder factories that accept `video/x-raw`. Common causes:

1. **WHIP server not running** — `curl http://127.0.0.1:8889/live/whip` should return HTTP (not connection refused)
2. **`CapabilityBoundingSet=` (empty) in systemd unit** — drops all Linux capabilities, blocking GStreamer codec discovery. Do not set an empty `CapabilityBoundingSet`.
3. **`PrivateTmp=true` in systemd unit** — NDI SDK uses `/tmp` for IPC. Keep `PrivateTmp` disabled.
4. **GStreamer registry not writable** — the service user needs write access to `/var/cache/gstreamer-1.0`

---

### WHIP connection refused

```
error trying to connect: tcp connect error: Connection refused (os error 111)
```

The WHIP server is not running or not listening on the configured URL.

```bash
# For MediaMTX:
sudo systemctl status mediamtx
curl -v http://127.0.0.1:8889/live/whip
```

---

### WebRTC / ICE timeout (viewer can't connect)

```
deadline exceeded while waiting connection
```

MediaMTX is not advertising the correct ICE candidates for your network.
Add your server's IP to `/etc/mediamtx/mediamtx.yml`:

```yaml
webrtcLocalUDPAddress: <server-ip>:8189
```

Then open firewall port `8189/udp` if using ufw:
```bash
sudo ufw allow 8189/udp
```

---

### HLS broken play button in Safari / iOS

Safari does not support Opus audio in HLS streams. Use VLC instead:

1. Install VLC from the App Store
2. Open Network Stream: `http://<server-ip>:8888/live/index.m3u8`

---

### WHIP HTTP 401 / 403

- Verify `auth_token` in `config.toml` is correct and not expired
- Some servers expect the token as a URL query param instead of a header

---

### Service restarts in a tight loop

```bash
sudo journalctl -u ndi-to-whip -n 50 --no-pager | grep -E "error|WARN|Failed"
```

Common causes:
- NDI source name is wrong — run `--probe` to get the exact name
- WHIP URL is unreachable — `curl -v YOUR_WHIP_URL`
- `config.toml` syntax error — test with:
  ```bash
  python3 -c "import tomllib; tomllib.load(open('/etc/ndi_to_whip/config.toml','rb'))"
  ```

---

## Hardware Encoding

`whipclientsink` (`GstBaseWebRTCSink`) selects the H.264 encoder automatically via
GStreamer factory discovery — it picks the first available encoder that produces
`video/x-h264`. To use GPU encoding, install the relevant GStreamer plugin and
the sink will prefer it over software `x264enc`.

### Intel / AMD — VAAPI

```bash
sudo apt install gstreamer1.0-vaapi vainfo
vainfo   # must show VAProfileH264Main or VAProfileH264ConstrainedBaseline ... VAEntrypointEncSlice

# Add service user to render group (required for GPU access)
sudo usermod -aG render ndi-whip
sudo systemctl restart ndi-to-whip
```

### NVIDIA — NVENC

```bash
nvidia-smi                              # verify GPU is present
gst-inspect-1.0 nvh264enc              # must print element info

# If nvh264enc is missing:
sudo apt install gstreamer1.0-plugins-bad
sudo systemctl restart ndi-to-whip
```

To verify which encoder was selected:

```bash
GST_DEBUG=GstBaseWebRTCSink:5 sudo journalctl -u ndi-to-whip -f
```

---

## Known Limitations

| Limitation | Impact | Workaround |
|---|---|---|
| NDI SDK is proprietary | Manual install required | Script fails clearly and prints the download URL |
| `gst-plugins-rs` requires Rust build | No Ubuntu package; 5–15 min build | Build once, pin the git tag |
| gst-plugins-rs API changed in 1.24 | `sig-server-url`, `ndidemux`, pre-encoded input all removed | Use `signaller::whip-endpoint`, `ndisrcdemux`, raw A/V input |
| `GstBaseWebRTCSink` controls encoding | `encoder` and `bitrate_kbps` config fields are hints; actual bitrate is WebRTC-negotiated | Set `video-caps="video/x-h264"` to constrain codec selection |
| Safari iOS rejects Opus in HLS | HLS broken play button on iPhone/iPad | Use VLC, or the WebRTC player at `:8889` |
| WebRTC ICE may fail across subnets | Viewer can't connect | Set `webrtcLocalUDPAddress` in mediamtx.yml |
| WHIP ICE without TURN fails behind symmetric NAT | No media after signalling | Add TURN server to `[whip] turn_server` |
| NDI SDK licence restricts redistribution | Cannot package `libndi.so` in a .deb | Automate install post-SDK-download only |

---

## File Reference

```
ndi_to_whip/
├── install.sh            Full system installer (run as root)
├── ndi_to_whip.py        Main Python application
├── config.toml           Configuration — edit before first run
├── mediamtx.yml          MediaMTX config for local WHIP server
├── ndi-to-whip.service   systemd unit file
├── validate.sh           Pre-flight checklist and troubleshooting
└── README.md             This file
```
