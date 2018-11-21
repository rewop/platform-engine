import subprocess
import sys

import click

from kubernetes import client as kube_client, config as kube_config


def make_env_file(psql_host, psql_port, env_file):
    """
    Sets up development environment to run platform engine.
    It creates a .env file with the value to be used to run the engine.
    """

    from shutil import which

    # Make sure kubectl is installed
    if which('kubectl') is None:
        print(('Missing kubectl cli. Follow '
               'https://kubernetes.io/docs/tasks/tools/install-kubectl '
               'to install it first.'), file=sys.stderr)
        exit(1)

    # Load configuration from default
    kube_config.load_kube_config()
    kube_config_inst = kube_config.kube_config.Configuration()

    # CLUSTER_HOST
    host = kube_config_inst.host
    click.echo(f'ATTENTION: resources will be created in cluster at {host}')
    click.confirm('Do you want to continue?', abort=True)

    v1_client = kube_client.CoreV1Api()

    # Create namespace
    md = kube_client.V1ObjectMeta(name='asyncy-system')
    ns = kube_client.V1Namespace(
        api_version='v1', kind='Namespace', metadata=md)
    try:
        v1_client.create_namespace(ns)
    except kube_client.rest.ApiException as e:
        # Check if namespace already exists
        if e.reason != 'Conflict':
            raise(e)

    # Create resources needed in the cluster
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
    cert_file = open(kube_config_inst.cert_file, 'r')
    cert = cert_file.read().replace('\n', '\\n')

    # CLUSTER_AUTH_TOKEN
    res = v1_client.list_namespaced_secret(
        'default', pretty='true')
    secret = next(x for x in res.items
                  if x.metadata.annotations
                  .get('kubernetes.io/service-account.name') == 'engine')
    token = secret.data.get('token')

    # POSTGRES
    # Defaults in config.py to use 'asyncy' db instead of
    # 'postgres'
    postgres_options = ('options=--search_path='
                        'app_public,app_hidden,app_private,public '
                        'dbname=asyncy '
                        'user=postgres '
                        f'host={psql_host} '
                        f'port={psql_port} ')

    env_file.write(f'CLUSTER_HOST={host}\n')
    env_file.write(f'CLUSTER_CERT={cert}\n')
    env_file.write(f'CLUSTER_AUTH_TOKEN={token}\n')
    env_file.write(f'POSTGRES={postgres_options}\n')
    env_file.write(f'LOGGER_LEVEL=debug\n')

    env_file.close()
