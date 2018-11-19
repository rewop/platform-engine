# -*- coding: utf-8 -*-
import asyncio
import os
import signal
import sys

import click

import prometheus_client

import tornado
from tornado import web
from kubernetes import config as kube_config, client as kube_client

from . import Version
from .Apps import Apps
from .Config import Config
from .Logger import Logger
from .Sentry import Sentry
from .http_handlers.StoryEventHandler import StoryEventHandler
from .processing.Services import Services
from .processing.internal import File, Http, Json, Log

_ONE_DAY_IN_SECONDS = 60 * 60 * 24

config = Config()
server = None
logger = Logger(config)
logger.start()


class Service:

    @click.group()
    def main():
        pass

    @staticmethod
    @main.command()
    @click.option('--port',
                  help='Set the port on which the HTTP server binds to',
                  default=os.getenv('PORT', '8084'))
    @click.option('--prometheus_port',
                  help='Set the port on which metrics are exposed',
                  default=os.getenv('METRICS_PORT', '8085'))
    @click.option('--sentry_dsn',
                  help='Sentry DNS for bug collection.',
                  default=os.getenv('SENTRY_DSN'))
    @click.option('--release',
                  help='The version being released (provide a Git commit ID)',
                  default=os.getenv('RELEASE_VER'))
    @click.option('--debug',
                  help='Sets the engine into debug mode',
                  default=False)
    def start(port, debug, sentry_dsn, release, prometheus_port):
        global server

        Services.set_logger(logger)

        # Init internal services.
        File.init()
        Log.init()
        Http.init()
        Json.init()
        Services.log_internal()

        logger.log('service-init', Version.version)
        signal.signal(signal.SIGTERM, Service.sig_handler)
        signal.signal(signal.SIGINT, Service.sig_handler)

        web_app = tornado.web.Application([
            (r'/story/event', StoryEventHandler, {'logger': logger})
        ], debug=debug)

        config.ENGINE_PORT = port

        server = tornado.httpserver.HTTPServer(web_app)
        server.listen(port)

        prometheus_client.start_http_server(port=int(prometheus_port))

        logger.log('http-init', port)

        loop = asyncio.get_event_loop()
        loop.create_task(Service.init_wrapper(sentry_dsn, release))

        tornado.ioloop.IOLoop.current().start()

        logger.log_raw('info', 'Shutdown complete!')

    @staticmethod
    @main.command()
    def dev_setup():
        kube_config.load_kube_config()
        kube_config_inst = kube_config.kube_config.Configuration()

        # CLUSTER_HOST
        click.echo(f"CLUSTER_HOST={kube_config_inst.host}")

        # CLUSTER_CERT
        cert_file = open(kube_config_inst.cert_file, "r")
        cert = cert_file.read().replace("\n", "\\n")
        click.echo(f"CLUSTER_CERT={cert}")

        # CLUSTER_AUTH_TOKEN
        v1Client = kube_client.CoreV1Api()
        ret = v1Client.list_namespaced_secret(
            'default', pretty='true')
        token = ret.items[0].data.get('token')
        click.echo(f"CLUSTER_AUTH_TOKEN={token}")

    async def init_wrapper(sentry_dsn: str, release: str):
        try:
            await Apps.init_all(sentry_dsn, release, config, logger)
        except BaseException as e:
            Sentry.capture_exc(e)
            logger.error(f'Failed to init apps!', exc=e)
            sys.exit(1)

    @staticmethod
    def sig_handler(*args, **kwargs):
        logger.log_raw('info', f'Signal {args[0]} received.')
        tornado.ioloop.IOLoop.instance().add_callback(Service.shutdown)

    @classmethod
    async def shutdown_app(cls):
        logger.log_raw('info', 'Unregistering with the gateway...')
        await Apps.destroy_all()  # All exceptions are handled inside.

        io_loop = tornado.ioloop.IOLoop.instance()
        io_loop.stop()
        loop = asyncio.get_event_loop()
        loop.stop()

    @classmethod
    def shutdown(cls):
        logger.log_raw('info', 'Shutting down...')

        server.stop()
        loop = asyncio.get_event_loop()
        loop.create_task(cls.shutdown_app())
