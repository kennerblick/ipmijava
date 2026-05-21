FROM debian:bookworm-slim

# System tools + X11 + VNC + Java + websockify
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
        ipmitool nmap curl \
        xvfb x11vnc \
        openjdk-17-jre \
        python3-websockify \
        procps \
    && rm -rf /var/lib/apt/lists/*

# Patch java.security in-container: remove SHA1 from disabled algorithm lists
# so ATEN iKVM JARs (signed with SHA1withRSA) can run.
# Using python3 for reliable multi-line property handling.
RUN python3 - <<'EOF'
import glob, re
for path in glob.glob('/usr/lib/jvm/**/security/java.security', recursive=True):
    txt = open(path).read()
    for prop in ('jdk.certpath.disabledAlgorithms', 'jdk.jar.disabledAlgorithms'):
        # Collapse line continuations, patch the property, restore
        def patch(m):
            val = m.group(2).replace('\\\n', ' ')
            parts = [p.strip() for p in val.split(',') if not p.strip().startswith('SHA1')]
            return m.group(1) + ', '.join(p for p in parts if p)
        txt = re.sub(r'(?m)(^' + re.escape(prop) + r'\s*=\s*)((?:[^\n]|\\\n)+)', patch, txt)
    open(path, 'w').write(txt)
    print(f'Patched {path}')
EOF

WORKDIR /app

# Python deps (--break-system-packages needed on Debian Bookworm / PEP 668)
COPY backend/requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# noVNC v1.4.0 static files (served by Flask at /novnc/)
RUN curl -sL https://github.com/novnc/noVNC/archive/refs/tags/v1.4.0.tar.gz \
    | tar xz -C /tmp && \
    mkdir -p /app/frontend/novnc && \
    cp -r /tmp/noVNC-1.4.0/core   /app/frontend/novnc/ && \
    cp -r /tmp/noVNC-1.4.0/vendor /app/frontend/novnc/ && \
    cp    /tmp/noVNC-1.4.0/vnc.html /app/frontend/novnc/ && \
    rm -rf /tmp/noVNC-1.4.0

# Bootstrap 5 + Icons (local, no CDN at runtime)
RUN mkdir -p /app/frontend/lib/fonts && \
    curl -sLf "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" \
         -o /app/frontend/lib/bootstrap.min.css && \
    curl -sLf "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js" \
         -o /app/frontend/lib/bootstrap.bundle.min.js && \
    curl -sLf "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" \
         -o /app/frontend/lib/bootstrap-icons.min.css && \
    curl -sLf "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff2" \
         -o /app/frontend/lib/fonts/bootstrap-icons.woff2 && \
    curl -sLf "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff" \
         -o /app/frontend/lib/fonts/bootstrap-icons.woff && \
    sed -i 's|\.\./fonts/|fonts/|g' /app/frontend/lib/bootstrap-icons.min.css

COPY backend/  ./backend/
COPY frontend/ ./frontend/

ENV CONFIG_PATH=/config/servers.json

EXPOSE 9193
# websockify WebSocket ports — one per concurrent KVM session
EXPOSE 6080-6089

# Single worker + threads so kvm.py session dict is shared across requests
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:9193", \
     "--timeout", "120", "backend.app:app"]
