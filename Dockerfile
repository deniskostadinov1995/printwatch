# Bambu P1S -> HTTP bridge. Deliberately tiny: the only dependency beyond the
# stdlib is paho-mqtt for the printer's TLS-MQTT telemetry feed. The camera
# protocol is raw sockets, so no OpenCV/ffmpeg is needed here -- all inference
# happens in the ml_api container.
FROM python:3.12-slim

RUN pip install --no-cache-dir paho-mqtt==2.1.0

WORKDIR /app
COPY bambu_bridge.py /app/

EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/healthz',timeout=4).status==200 else 1)"

CMD ["python", "-u", "bambu_bridge.py"]
