# Dockerfile for NDI → WHIP bridge
# Builds gst-plugin-webrtc (whipclientsink) and packages the Python app.
# This Dockerfile downloads and installs the NDI SDK during the build.
# The NDI SDK is proprietary; this script accepts the EULA non-interactively
# (pipes `yes`). If you prefer not to bake the SDK into the image, remove
# the NDI install RUN and mount the host's /usr/local at runtime instead.

ARG GST_PLUGINS_RS_REV=gstreamer-1.24.13
FROM ubuntu:24.04 AS builder
ARG GST_PLUGINS_RS_REV
ENV DEBIAN_FRONTEND=noninteractive

# Install system packages required for build and runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
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
    build-essential \
    pkg-config \
    cmake \
    ninja-build \
    meson \
    git \
    curl \
    wget \
    ca-certificates \
    nasm \
    python3 \
    python3-venv \
    python3-pip \
    python3-gi \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    libx264-dev \
    libopus-dev \
 && rm -rf /var/lib/apt/lists/*

# Install Rust toolchain (rustup) to /opt for deterministic path
ENV CARGO_HOME=/opt/cargo
ENV RUSTUP_HOME=/opt/rustup
ENV PATH=/opt/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal

# Download and install NDI SDK (accept EULA non-interactively)
WORKDIR /tmp
RUN curl -s https://downloads.ndi.tv/SDK/NDI_SDK_Linux/Install_NDI_SDK_v6_Linux.tar.gz \
   | tar xvz -C /tmp/ && \
   yes y | bash /tmp/Install_NDI_SDK_v6_Linux.sh > /dev/null || true && \
   # If installer left an extracted folder, move it to /tmp/sdk
   if [ -d "/tmp/NDI SDK for Linux" ]; then mv "/tmp/NDI SDK for Linux" /tmp/sdk; fi && \
   # Copy headers/libs into /usr/local so cargo/build can find them
   mkdir -p /usr/local/include /usr/local/lib /usr/local/bin && \
   cp -r /tmp/sdk/include/* /usr/local/include/ 2>/dev/null || true && \
   cp -a /tmp/sdk/lib/x86_64-linux-gnu/* /usr/local/lib/ 2>/dev/null || true && \
   cp -a /tmp/sdk/bin/x86_64-linux-gnu/* /usr/local/bin/ 2>/dev/null || true && \
   rm -rf /tmp/sdk /tmp/Install_NDI_SDK_v6_Linux.sh || true

# Build gst-plugins-rs (NDI + WebRTC plugins)
RUN git clone --branch ${GST_PLUGINS_RS_REV} --depth 1 https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git
WORKDIR /tmp/gst-plugins-rs
RUN cargo build --release --package gst-plugin-ndi --package gst-plugin-webrtc

# Install built plugin libs into a staging directory
RUN mkdir -p /staging/usr/local/lib/gstreamer-1.0 \
 && find target/release -name "libgstrswebrtc.so" -exec cp {} /staging/usr/local/lib/gstreamer-1.0/ \; \
 && find target/release -name "libgstndi.so" -exec cp {} /staging/usr/local/lib/gstreamer-1.0/ \; || true

# Final image
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

ARG GST_PLUGINS_RS_REV
ENV GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0
ENV LD_LIBRARY_PATH=/usr/local/lib

# Runtime packages
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-nice \
    libgstreamer1.0-0 \
    libnice10 \
    libssl3 \
    libsrtp2-1 \
    python3 \
    python3-venv \
    python3-pip \
    python3-gi \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    libx264-dev \
    libopus0 \
 && rm -rf /var/lib/apt/lists/*

# Copy prebuilt plugins from builder stage
COPY --from=builder /staging/usr/local/lib/gstreamer-1.0/ /usr/local/lib/gstreamer-1.0/

# Copy plugin verification script and run it to ensure elements registered
COPY scripts/check_gst_plugins.sh /usr/local/bin/check_gst_plugins.sh
RUN chmod +x /usr/local/bin/check_gst_plugins.sh && \
   /usr/local/bin/check_gst_plugins.sh

# Copy application files
WORKDIR /opt/ndi_to_whip
COPY . /opt/ndi_to_whip

# Install Python dependencies in a venv
RUN python3 -m venv --system-site-packages /opt/ndi_to_whip/venv \
 && /opt/ndi_to_whip/venv/bin/pip install --upgrade pip \
 && /opt/ndi_to_whip/venv/bin/pip install tomli structlog

# Create non-root service user
RUN useradd --system --no-create-home --shell /usr/sbin/nologin ndi-whip || true \
 && mkdir -p /var/log/ndi_to_whip /etc/ndi_to_whip \
 && chown -R ndi-whip:ndi-whip /opt/ndi_to_whip /var/log/ndi_to_whip /etc/ndi_to_whip

# Install default config if not provided (copied from repo)
RUN if [ ! -f /etc/ndi_to_whip/config.toml ]; then cp /opt/ndi_to_whip/config.toml /etc/ndi_to_whip/config.toml; fi

VOLUME ["/usr/local/lib:/usr/local/lib:ro"]

# Note: The NDI SDK (libndi.so and headers) must be available under /usr/local
# at runtime. Two options:
#  - Mount the host /usr/local (read-only) that contains the NDI SDK: 
#      docker run -v /usr/local:/usr/local:ro ...
#  - Build NDI SDK into the image (not included here due to licensing).

USER ndi-whip
WORKDIR /opt/ndi_to_whip

ENTRYPOINT ["/opt/ndi_to_whip/venv/bin/python", "/opt/ndi_to_whip/ndi_to_whip.py", "--config", "/etc/ndi_to_whip/config.toml"]
