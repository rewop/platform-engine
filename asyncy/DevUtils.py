import subprocess
import sys

import click

from kubernetes import client as kube_client, config as kube_config

namespace_name = 'asyncy-system';

def ensure_db_initialized(postgres_options):
    import psycopg2

    conn = psycopg2.connect(postgres_options)
    cur = conn.cursor()
    cur.execute('select exists(select * from information_schema.tables where table_schema=\'app_public\' and table_name=\'releases\')')
    if not cur.fetchone()[0]:
        click.echo(('Database not initialized. '
               'First initialize it by following '
               'https://github.com/asyncy/platform-bootstrap'), file=sys.stderr)
        exit(1)

def ensure_kubectl_installed():
    from shutil import which

    if which('kubectl') is None:
        click.echo(('Missing kubectl cli. Follow '
               'https://kubernetes.io/docs/tasks/tools/install-kubectl '
               'to install it first.'), file=sys.stderr)
        exit(1)

def create_k8s_namespace(k8s_client):
    md = kube_client.V1ObjectMeta(name=namespace_name)
    ns = kube_client.V1Namespace(
        api_version='v1', kind='Namespace', metadata=md)
    try:
        k8s_client.create_namespace(ns)
    except kube_client.rest.ApiException as e:
        # Check if namespace already exists
        if e.reason != 'Conflict':
            raise(e)

def create_k8s_resources():
    # Create resources needed in the cluster
    kube_resources = [
        'service_accounts/engine.yaml',
        'secrets/engine.yaml',
        'role_bindings/engine.yaml',
    ]
    command = ('curl -s '
               'https://raw.githubusercontent.com/asyncy/stack-kubernetes/'
               'master/kubernetes-pre-init/{resource}'
               f' | kubectl apply --namespace {namespace_name} -f -')
    for resource in kube_resources:
        click.echo(f'creating or updating resource {resource}')
        resp = subprocess.run(command.format(
            resource=resource), shell=True)
        resp.check_returncode()

def make_env_file(psql_host, psql_port, env_file):
    """
    Sets up development environment to run platform engine.
    It creates a .env file with the value to be used to run the engine.
    """

        # POSTGRES
    # Defaults in config.py to use 'asyncy' db instead of
    # 'postgres'
    postgres_options = ('options=--search_path='
                        'app_public,app_hidden,app_private,public '
                        'dbname=asyncy '
                        'user=postgres '
                        f'host={psql_host} '
                        f'port={psql_port} ')

    ensure_db_initialized(postgres_options)
    ensure_kubectl_installed()

    # Load configuration from default
    kube_config.load_kube_config()
    kube_config_inst =  kube_config.kube_config.Configuration()

    # CLUSTER_HOST
    host = kube_config_inst.host
    click.echo(f'ATTENTION: resources will be created in cluster at {host}')
    click.confirm('Do you want to continue?', abort=True)

    v1_client = kube_client.CoreV1Api()

    create_k8s_namespace(k8s_client=v1_client)
    create_k8s_resources()

    # CLUSTER_CERT
    cert_file = open(kube_config_inst.cert_file, 'r')
    cert = cert_file.read().replace('\n', '\\n')

    # CLUSTER_AUTH_TOKEN
    res = v1_client.list_namespaced_secret(
        namespace_name, pretty='true')

    secret = next(x for x in res.items
                  if x.metadata.annotations
                  .get('kubernetes.io/service-account.name') == 'engine')
    token = secret.data.get('token')

    env_file.write(f'CLUSTER_HOST={host}\n')
    env_file.write(f'CLUSTER_CERT={cert}\n')
    env_file.write(f'CLUSTER_AUTH_TOKEN={token}\n')
    env_file.write(f'POSTGRES={postgres_options}\n')
    env_file.write(f'LOGGER_LEVEL=debug\n')

    env_file.close()
