"""Run the web process: a single-threaded wsgiref server in front of the recorder."""
import functools
import signal
import sys
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .. import protocol
from .app import App


class _Handler(WSGIRequestHandler):
    timeout = 30  # seconds per connection, so a stalled client cannot hold the server

    def log_message(self, format, *args):
        sys.stderr.write("web: %s %s\n" % (self.address_string(), format % args))


def _exit(signum, frame):
    raise SystemExit(0)


def serve(config, socket_path):
    signal.signal(signal.SIGTERM, _exit)
    app = App(config, functools.partial(protocol.call, socket_path))
    httpd = make_server(config["bind"], int(config["port"]), app,
                        server_class=WSGIServer, handler_class=_Handler)
    print("web: listening on http://%s:%d" % (config["bind"], int(config["port"])), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
