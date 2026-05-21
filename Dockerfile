FROM python:3.12-alpine

# System tools: ipmitool for IPMI commands, nmap for network scan, curl for Bootstrap download
RUN apk add --no-cache ipmitool nmap curl

WORKDIR /app

# Python deps first (layer caching)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bundle Bootstrap 5 + Bootstrap Icons locally (no CDN at runtime)
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

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:9193", "--timeout", "120", "backend.app:app"]
