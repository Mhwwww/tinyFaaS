from __future__ import annotations

import zipfile
import io
import base64
import docker
import json
import shutil
import sys
import shutil
import hashlib
import os
import time
import requests
import logging
import typing
import urllib
import urllib.request
import uuid

import tornado.escape
import tornado.ioloop
import tornado.web

CONFIG_PORT = 8080

management_service: ManagementService

class FunctionHandler(object):
    def __init__(
            self,
            function_name: str,
            function_resource: str,
            function_env: str,
            function_threads: int,
            zip_hash: str,
            file_path: str
        ):

        logging.debug(f'creating new function handler for {function_name}')

        self.client = docker.from_env()
        self.function_resource = function_resource
        self.name = function_name
        self.zip_hash = zip_hash

        # create handler runtime image
        # create a temporary directory
        path = os.path.join("./tmp", str(uuid.uuid4()))

        os.makedirs(path, exist_ok=True)

        fn_path = os.path.join(path, "fn")

        os.makedirs(fn_path, exist_ok=True)

        # copy function stuff into fn_path
        shutil.copytree(file_path, fn_path, dirs_exist_ok=True)

        # copy base stuff into the path
        shutil.copytree(f'./handler-runtime/{function_env}', path, dirs_exist_ok=True)

        self.this_image = self.client.images.build(path=path, rm=True)[0]

        self.thread_count = function_threads

        # connect handler container(s) to endpoint on a dedicated subnet
        self.this_network = self.client.networks.create(self.name + '-net', driver='bridge')

        self.this_network.connect(management_service.endpoint_container.name)

        # connect metacontainer to make health check
        self.this_network.connect(management_service.meta_container)

        self.this_handler_ips: typing.List[str] = []
        # create handler container(s)
        self.this_containers: typing.List[docker.models.containers.Container] = []

        for i in range(0, self.thread_count):
            self.this_containers.append(self.client.containers.run(self.this_image, network=self.this_network.name, detach=True, remove=True, name=function_name + '-' + str(i)))
            # getting IP address of the handler container by inspecting the network and converting CIDR to IPv4 address notation (very dirtily, removing the last 3 chars -> i.e. '/20', so let's hope we don't have a /8 subnet mask)
            self.this_handler_ips.append(docker.APIClient().inspect_network(self.this_network.id)['Containers'][self.this_containers[i].id]['IPv4Address'][:-3])

        # tell endpoint about new function
        function_handler = {
            'function_resource': self.function_resource,
            'function_containers': self.this_handler_ips,
        }

        data = json.dumps(function_handler).encode('ascii')

        # wait for the containers to be up and running
        for container in self.this_containers:
            container.reload()
            count = 0
            while container.status != 'running':
                count += 1
                logging.debug(f'waiting for {container.name} to be up and running... ({count})')
                container.reload()
                time.sleep(0.1)

        # wait for /health to be available
        for ip in self.this_handler_ips:
            count = 0
            while True:
                count += 1
                try:
                    urllib.request.urlopen(url=f'http://{ip}:8000/health', timeout=0.1)
                    break
                except Exception as e:
                    logging.debug(f'waiting for server at {ip} to be healthy... ({count})')
                    time.sleep(0.1)

        urllib.request.urlopen(url='http://' + management_service.endpoint_container_ipaddr + ':80', data=data)

        shutil.rmtree(path, ignore_errors=True)

    def destroy(self) -> None:

        logging.debug('destroying function handler')

        function_handler = {
            'function_resource': self.function_resource,
            'function_containers': []
        }
        data = json.dumps(function_handler).encode('ascii')
        urllib.request.urlopen(url='http://' + management_service.endpoint_container_ipaddr + ':80', data=data)


        for container in self.this_containers:
            container.remove(force=True)
        self.this_network.reload()
        for container in self.this_network.containers:
            self.this_network.disconnect(container, force=True)
        self.this_network.remove()

class UploadHandler(tornado.web.RequestHandler):
    def post(self) -> None:
        try:
            # expected post body
            # {
            #     name: "name",
            #     threads: 2,
            #     env: "python3" | "nodejs", | "binary",
            #     zip: "base64-zip"
            # }
            #

            logging.debug(self.request.body)

            b = tornado.escape.json_decode(self.request.body)

            logging.debug(f"got request to create function {b['name']} with {b['threads']} threads")

            name = b['name'] + '-handler'
            threads = b['threads']
            env = b['env']
            path = b['name']
            resource = '/' + b['name']
            funczip = base64.b64decode(b['zip'], validate=False)

            management_service.create_function(name=name, env=env, threads=threads, path=path, resource=resource, funczip=funczip)

            self.write(f"created function {name} with {threads} threads")
            self.flush()

        except Exception as e:
            logging.error(f"error while creating function: {e}")
            self.set_status(500)
            self.write(f"error while creating function: {e}")
            self.finish()
            raise e

class URLUploadHandler(tornado.web.RequestHandler):
    def post(self) -> None:
        try:
            # expected post body
            # {
            #     name: "name",
            #     threads: 2,
            #     url: "https://github.com/OpenFogStack/tinyFaaS/archive/master.zip",
            #     env: "python3" | "nodejs", | "binary"
            #     subfolder_path: "/examples/sieve-of-erasthostenes"
            # }

            logging.debug(self.request.body)

            b = tornado.escape.json_decode(self.request.body)

            logging.debug(f"got request to create function {b['name']} with f{b['threads']} threads")

            name = b['name'] + '-handler'
            threads = b['threads']
            env = b['env']
            path = b['name']
            resource = '/' + b['name']
            funcurl = b['url']

            subfolder_path = None

            if 'subfolder_path' in b:
                subfolder_path = b['subfolder_path']

            r = requests.get(funcurl, allow_redirects=True)

            funczip = r.content

            management_service.create_function(name=name, env=env, threads=threads, path=path, resource=resource, funczip=funczip, subfolder_path=subfolder_path)

            self.finish(f"created function {name} with {threads} threads")

        except Exception as e:
            logging.error(f"error while creating function: {e}")
            self.set_status(500)
            self.finish(f"error while creating function: {e}")
            raise e

class DeleteHandler(tornado.web.RequestHandler):
    def post(self) -> None:
        try:
            name = self.request.body.decode('utf-8')
            logging.debug(f'got request to delete function {name}')
            handler_name =  name + '-handler'
            # expected json {name: "function-name-from-pkg-json"}
            if handler_name in management_service.function_handlers:
                management_service.function_handlers[handler_name].destroy()
                del management_service.function_handlers[handler_name]

                self.finish(f"deleted function {name}")

            else:
                self.write('Not found')

        except Exception as e:
            logging.error(f"error while deleting function: {e}")
            self.set_status(500)
            self.finish(f"error while deleting function: {e}")
            raise e

class WipeHandler(tornado.web.RequestHandler):
    def post(self) -> None:
        logging.debug('got request to wipe all functions')
        try:
            for f in management_service.function_handlers:
                management_service.function_handlers[f].destroy()
            management_service.function_handlers.clear()
            self.finish("wiped all functions")

        except Exception as e:
            logging.error(f"error while wiping functions: {e}")
            self.set_status(500)
            self.finish(f"error while wiping functions: {e}")
            raise e


class ListHandler(tornado.web.RequestHandler):
    def get(self) -> None:
        logging.debug('got request to list handlers')
        try:
            out = []
            for f in management_service.function_handlers:
                out.append({'name': management_service.function_handlers[f].name[:-len('-handler')],
                'hash': management_service.function_handlers[f].zip_hash,
                'threads': management_service.function_handlers[f].thread_count,
                'resource': management_service.function_handlers[f].function_resource})
            self.finish(json.dumps(out) + '\n')

        except Exception as e:
            logging.error(f"error while listing functions: {e}")
            self.set_status(500)
            self.finish(f"error while listing functions: {e}")
            raise e



class LogsHandler(tornado.web.RequestHandler):
    def get(self) -> None:
        logging.debug('got request to get logs')
        try:
            for f in management_service.function_handlers:
                handler = management_service.function_handlers[f]
                for cont in handler.this_containers:
                    self.write(cont.logs())
            self.finish()

        except Exception as e:
            logging.error(f"error while getting logs: {e}")
            self.set_status(500)
            self.finish(f"error while getting logs: {e}")
            raise e


class ManagementService():
    def __init__(
        self,
        meta_container: str
    ):
        self.meta_container = meta_container

        self.endpoint_container: docker.models.containers.Container = None
        self.endpoint_container_ipaddr: str = ''
        self.function_handlers: typing.Dict[str, FunctionHandler] = {}

    def start(self) -> None:
        # create endpoint
        logging.debug('ManagementService: creating endpoint container')
        self.create_endpoint(meta_container, coapPort=coapPort, httpPort=httpPort, grpcPort=grpcPort)

        # accept incoming configuration requests and create handlers based on that

        logging.debug('ManagementService: creating tornado handlers')

        app = tornado.web.Application([
            (r'/upload', UploadHandler),
            (r'/delete', DeleteHandler),
            (r'/list', ListHandler),
            (r'/wipe', WipeHandler),
            (r'/logs', LogsHandler),
            (r'/uploadURL', URLUploadHandler)
        ])

        logging.debug('ManagementService: starting tornado server')

        app.listen(CONFIG_PORT)
        tornado.ioloop.IOLoop.current().start()

    def create_endpoint(self, meta_container: str, coapPort: int, httpPort: int, grpcPort: int) -> None:
        """
        Creates the reverse proxy, attaches it to the management service (this) and
        starts the proxy with the configured ports.
        """

        # get the docker client
        client = docker.from_env()

        logging.debug('building endpoint container image')

        # build the reverse proxy image
        endpoint_image = client.images.build(tag='tinyfaas-reverse-proxy', path='./reverse-proxy/', rm=True)[0]

        logging.debug('removing old endpoint networks')

        # remove old endpoint-net networks
        for n in client.networks.list(names=['endpoint-net']):
            n.remove()

        logging.debug('creating endpoint network')

        # create new endpoint-net network
        self.endpoint_network = client.networks.create('endpoint-net', driver='bridge')

        # find out which ports to forward
        # if a port is set to zero, we just don't use it
        ports: typing.Dict[str, int] = {}
        if coapPort > 0:
            ports['6000/udp'] = coapPort

        if httpPort > 0:
            ports['7000/tcp'] = httpPort

        if grpcPort > 0:
            ports['8000/tcp'] = grpcPort

        logging.debug('creating endpoint container')

        # create the endpoint container
        self.endpoint_container = client.containers.run(endpoint_image, network=self.endpoint_network.name, ports=ports, detach=True, remove=True, name='tinyfaas-reverse-proxy')

        # getting IP address of the handler container by inspecting the network and converting CIDR to IPv4 address notation (very dirtily, removing the last 3 chars -> i.e. '/20', so let's hope we don't have a /8 subnet mask)
        self.endpoint_container_ipaddr = docker.APIClient().inspect_network(self.endpoint_network.id)['Containers'][self.endpoint_container.id]['IPv4Address'][:-3]
        logging.debug(f'endpoint container ip address: f{self.endpoint_container_ipaddr}')

        logging.debug('connecting meta_container to endpoint network')

        # add our container to the endpoint-net network
        self.endpoint_network.connect(meta_container)

    def create_function(self, name: str, env: str, threads: int, path: str, resource: str, funczip: bytes, subfolder_path: typing.Optional[str]=None) -> None:
        """
        Creates a function container and attaches it to the endpoint net network.
        """

        logging.debug(f'creating function {name}')

        path = os.path.join('./tmp', str(uuid.uuid4()))
        os.makedirs(path, exist_ok=True)

        try:
            # funczip is a zip file containing the function code
            f = zipfile.ZipFile(io.BytesIO(funczip))

            logging.debug(f'unzipping function to {path}')

            # extract the function code to the tmp directory
            f.extractall(path=path)

            # maybe we need to go into a subfolder within this directory
            if subfolder_path is not None:
                path = os.path.join(path, subfolder_path)

            # if we know this function already, we need to update it in-place
            if name in self.function_handlers:
                logging.debug(f'function {name} exists already, destroying and recreating')
                self.function_handlers[name].destroy()

            logging.debug(f'creating function handler for {name}')

            # create a new function handler
            self.function_handlers[name] = FunctionHandler(function_name=name, function_resource=resource, function_env=env, function_threads=threads, zip_hash=hashlib.sha256(base64.b64encode(funczip)).hexdigest(), file_path=path)

            shutil.rmtree(path)

        except Exception as e:
                raise e

if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise ValueError('Too many or too few arguments provided:\n' + json.dumps(sys.argv) + '\nUsage: management-service.py [tinyfaas-mgmt container name] <endpoint port>')

    logging.basicConfig(level=logging.DEBUG)
    # logging.basicConfig(level=logging.INFO)

    meta_container = sys.argv[1]

    logging.debug(f'Meta container is {meta_container}')

    # default coap port is 5683
    coapPort = int(os.getenv('COAP_PORT', '5683'))

    logging.debug(f'COAP port is {coapPort}')

    # http port
    httpPort = int(os.getenv('HTTP_PORT', '80'))

    logging.debug(f'HTTP port is {httpPort}')

    # grpc port
    grpcPort = int(os.getenv('GRPC_PORT', '8000'))

    logging.debug(f'GRPC port is {grpcPort}')

    logging.debug(f'Getting meta container f{meta_container} from Docker')
    try:
      c = docker.from_env().containers.get(meta_container)
      logging.debug(f'Meta container found: {c}')
    except:
      raise ValueError('Provided container name does not match a running container' + '\nUsage: management-service.py [tinyfaas-mgmt container name] <endpoint port>')

    logging.info('Creating management service')

    management_service = ManagementService(meta_container=meta_container)

    logging.debug('Starting management service')

    management_service.start()
