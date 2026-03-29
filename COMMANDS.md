# Useful Commands

## ndi-to-whip service

```bash
# Status
sudo systemctl status ndi-to-whip

# Start / Stop / Restart
sudo systemctl start ndi-to-whip
sudo systemctl stop ndi-to-whip
sudo systemctl restart ndi-to-whip

# Enable / Disable on boot
sudo systemctl enable ndi-to-whip
sudo systemctl disable ndi-to-whip

# Live logs
sudo journalctl -u ndi-to-whip -f

# Last 50 lines
sudo journalctl -u ndi-to-whip -n 50 --no-pager

# Errors only
sudo journalctl -u ndi-to-whip --no-pager | grep error
```

## MediaMTX service

```bash
# Status
sudo systemctl status mediamtx

# Start / Stop / Restart
sudo systemctl start mediamtx
sudo systemctl stop mediamtx
sudo systemctl restart mediamtx

# Enable / Disable on boot
sudo systemctl enable mediamtx
sudo systemctl disable mediamtx

# Live logs
sudo journalctl -u mediamtx -f

# Last 50 lines
sudo journalctl -u mediamtx -n 50 --no-pager
```

## Both services together

```bash
# Restart both
sudo systemctl restart mediamtx ndi-to-whip

# Watch both logs at once
sudo journalctl -u ndi-to-whip -u mediamtx -f
```

## Configuration

```bash
# Edit config
sudo nano /etc/ndi_to_whip/config.toml

# Edit MediaMTX config
sudo nano /etc/mediamtx/mediamtx.yml

# Apply config changes (restart required)
sudo systemctl restart ndi-to-whip
```

## Diagnostics

```bash
# Check stream is publishing (look for "is publishing to path 'live'")
sudo journalctl -u mediamtx --since "2 minutes ago" --no-pager

# Check HLS stream and bitrate
curl -s http://127.0.0.1:8888/live/index.m3u8

# Discover NDI sources on the network
/opt/ndi_to_whip/venv/bin/python /opt/ndi_to_whip/ndi_to_whip.py --probe

# Run pre-flight checklist
sudo bash /home/whip_ubuntu/NDI-to-WHIP/validate.sh

# Check GStreamer elements are available
GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0 gst-inspect-1.0 whipclientsink
GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0 gst-inspect-1.0 ndisrc
```

## Viewing the stream

| Method | URL |
|---|---|
| WebRTC (low latency) | `http://<server-ip>:8889/live/` |
| HLS (browser) | `http://<server-ip>:8888/live/` |
| VLC / iOS | `http://<server-ip>:8888/live/index.m3u8` |
| RTSP | `rtsp://<server-ip>:8554/live` |

```bash
# Get server IP
ip addr show | grep 'inet ' | grep -v 127.0.0.1
```
