"""
Worker de schedulers pesados — proceso separado del web server.

Corre los schedulers que saturan la DB (sync catálogo, inventory refresh,
stock prewarm, etc.) en un proceso independiente. El web server (gunicorn)
solo corre DLQ + pedidos sync y sirve HTTP sin competencia por DB.

Deploy en Railway como servicio separado con:
  startCommand = "python worker.py"
  HEAVY_SCHEDULERS = true
  SYNC_SCHEDULER = true

El web server tiene HEAVY_SCHEDULERS=false (default).
"""
import os
import sys
import time
import signal
import logging

# Force heavy schedulers ON for this worker
os.environ['HEAVY_SCHEDULERS'] = 'true'
# Disable DLQ + pedidos (ya corren en el web server)
os.environ.setdefault('WORKER_SKIP_ESSENTIAL', 'true')

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger('worker')

from app import create_app

app = create_app()

# Keep the process alive
_running = True

def _signal_handler(sig, frame):
    global _running
    logger.info('[WORKER] Signal %s recibido — cerrando gracefully', sig)
    _running = False

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

logger.info('[WORKER] Schedulers pesados activos — proceso independiente del web server')
logger.info('[WORKER] PID=%d — esperando señal de cierre (SIGTERM/SIGINT)', os.getpid())

# Health check: log alive cada 5 min
while _running:
    time.sleep(300)
    if _running:
        logger.info('[WORKER] Alive — schedulers corriendo')

logger.info('[WORKER] Proceso terminado')
