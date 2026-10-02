# gnnote: self-hosted converter (web UI with vendored Pyodide + POST /api/convert).
#
#   docker build -t gnnote .
#   docker run --rm -p 8000:8000 gnnote
#
# The build downloads the Pyodide runtime (about 15 MB) into dist/pyodide so the
# container works without internet access afterwards.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY . /app

# Pure-Python, stdlib-only package: no pip dependencies to install.  Build the static
# site (web/ + gnnote.zip + version.json) with the Pyodide files vendored.
RUN python scripts/build_web.py --vendor-pyodide --out dist \
    && find /app -name __pycache__ -type d -prune -exec rm -rf {} +

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import json,urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4); sys.exit(0 if json.load(r).get('ok') else 1)"

CMD ["python", "-m", "gnnote.server", "--host", "0.0.0.0", "--port", "8000", "--dist", "dist"]
