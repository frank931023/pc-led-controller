"""
Entry point of the Motion Control Center. The application itself is the `mcc` package.

  Docker:       gunicorn app:app            (see Dockerfile)
  Development:  python app.py               (http://localhost:5001)
"""

import logging
import os

from mcc import create_app

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

app = create_app()

if __name__ == '__main__':
    # debug=True exposes the Werkzeug debugger to the whole LAN (remote code execution),
    # so it is off unless FLASK_DEBUG=1 is set for local development.
    debug = os.getenv('FLASK_DEBUG', '0').lower() in ('1', 'true', 'yes')
    logging.getLogger('mcc').info('Starting Motion Control Center on :5001 (debug=%s)', debug)
    # threaded=True: a long calibrate on one device must not block requests for others.
    app.run(host='0.0.0.0', port=5001, debug=debug, threaded=True)
