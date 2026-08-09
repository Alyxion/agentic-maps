# Builds the agentic-maps-api image for the local "2-minute geo server"
# stack (docker-compose.yml). See docs/setup-guide.md for the full picture:
# this container talks to the `valhalla` and `nominatim` services over the
# compose network via AGENTIC_MAPS_VALHALLA_URL / AGENTIC_MAPS_NOMINATIM_URL
# and serves tiles/vector data/the demo page itself.
#
# Installs the `rest,fallback` extras — enough for the REST surface, tile
# harvesting/serving and the raster fallback ladder. Deliberately NOT
# `render`: that extra needs a real headless-Chromium binary
# (`playwright install chromium`, a separate multi-hundred-MB download) for
# `POST /render`, a feature this stack does not need. Add it yourself
# (extend this Dockerfile) if you do — see docs/rendering.md.
FROM python:3.11-slim

WORKDIR /app

# Copy only what `pip install -e .` and the runtime actually need — README
# and LICENSE because pyproject.toml's `readme` key resolves at install time.
COPY pyproject.toml README.md LICENSE ./
COPY agentic_maps ./agentic_maps
COPY web ./web

RUN pip install --no-cache-dir ".[rest,fallback]" "uvicorn[standard]>=0.30"

EXPOSE 8095

# AGENTIC_MAPS_MODE (set in .env / docker-compose.offline.yml) is read by
# devserver.main() as the container-friendly equivalent of --mode.
CMD ["python", "-m", "agentic_maps.devserver", "--host", "0.0.0.0", "--port", "8095"]
