import os
import tempfile

# app.py creates the app (and its DeviceStore) at import time: point it at a throw-away file first,
# and keep Beszel off so nothing tries to reach a hub.
os.environ['CONFIG_FILE'] = os.path.join(tempfile.mkdtemp(), 'pi_config.json')
os.environ['BESZEL_URL'] = ''
