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
import subprocess

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
    @click.option('--psql-host',
                  help='Hostname to connect to postgresql',
                  default='localhost')
    @click.option('--psql-port',
                  help='Hostname to connect to postgresql',
                  default='5432')
    @click.argument('env_file', type=click.File('w'), default='.env')
    def dev_setup(psql_host, psql_port, env_file):
        """Sets up development environment to run platform engine.
        It creates a .env file with the value to be ued to run the engine.
        """

        from shutil import which

        # Make sure kubectl is installed
        if which('kubectl') is None:
            print(('Missing kubectl cli. Follow '
                   'https://kubernetes.io/docs/tasks/tools/install-kubectl '
                   'to install it first.'), file=sys.stderr)
            exit(1)

        # load configuration from default
        kube_config.load_kube_config()
        kube_config_inst = kube_config.kube_config.Configuration()

        # CLUSTER_HOST
        host = kube_config_inst.host
        click.echo(f'ATTENTION: resources will be create in cluster at {host}')
        click.confirm('Do you want to continue?', abort=True)

        v1Client = kube_client.CoreV1Api()

        # create namespace
        md = kube_client.V1ObjectMeta(name='asyncy-system')
        ns = kube_client.V1Namespace(
            api_version='v1', kind='Namespace', metadata=md)
        try:
            v1Client.create_namespace(ns)
        except kube_client.rest.ApiException as e:
            # check if namespace already exists
            if e.reason != 'Conflict':
                raise(e)

        # Create resources needed in the cluster
        # @todo these resources will be created in the namespace default
        # Should we also create a namespace for the engine?
        kube_resources = [
            'service_accounts/engine.yaml',
            'secrets/engine.yaml',
            'role_bindings/engine.yaml',
        ]
        command = ('curl -s '
                   'https://raw.githubusercontent.com/asyncy/stack-kubernetes/'
                   'master/kubernetes-pre-init/{resource}'
                   ' | kubectl apply --namespace asyncy-system -f -')
        for resource in kube_resources:
            click.echo(f'creating or updating resource {resource}')
            resp = subprocess.run(command.format(
                resource=resource), shell=True)
            resp.check_returncode()

        # CLUSTER_CERT
        cert_file = open(kube_config_inst.cert_file, "r")
        cert = cert_file.read().replace("\n", "\\n")

        # CLUSTER_AUTH_TOKEN
        res = v1Client.list_namespaced_secret(
            'default', pretty='true')
        secret = next(x for x in res.items
                      if x.metadata.annotations
                      .get('kubernetes.io/service-account.name') == 'engine')
        token = secret.data.get('token')

        # POSTGRES
        # defaults in config.py to use 'asyncy' db instead of
        # 'postgres'
        postgres_options = ('options=--search_path='
                            'app_public,app_hidden,app_private,public '
                            'dbname=asyncy '
                            'user=postgres '
                            f'host={psql_host} '
                            f'port={psql_port} ')

        env_file.write(f'CLUSTER_HOST={host}\m')
        env_file.write(f'CLUSTER_CERT={cert}\n')
        env_file.write(f'CLUSTER_AUTH_TOKEN={token}\n')
        env_file.write(f'POSTGRES={postgres_options}\n')
        env_file.write(f'LOGGER_LEVEL=debug\n')

        env_file.close()

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
