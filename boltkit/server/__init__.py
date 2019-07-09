#!/usr/bin/env python
# coding: utf-8

# Copyright (c) 2002-2016 "Neo Technology,"
# Network Engine for Objects in Lund AB [http://neotechnology.com]
#
# This file is part of Neo4j.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from itertools import chain
from logging import getLogger
from math import ceil
from shlex import split as shlex_split
from textwrap import wrap
from threading import Thread
from time import sleep
from uuid import uuid4

from docker import DockerClient
from docker.errors import ImageNotFound

from boltkit.addressing import Address
from boltkit.auth import make_auth
from boltkit.client import AddressList, Connection
from boltkit.server.images import resolve_image


log = getLogger("boltkit")


class Neo4jMachineSpec:

    # Base config for all machines. This can be overridden by
    # individual instances.
    config = {
        "dbms.backup.enabled": "false",
        "dbms.memory.heap.initial_size": "300m",
        "dbms.memory.heap.max_size": "500m",
        "dbms.memory.pagecache.size": "50m",
        "dbms.transaction.bookmark_ready_timeout": "5s",
    }

    def __init__(self, name, service_name, bolt_port, http_port, config):
        self.name = name
        self.service_name = service_name
        self.bolt_port = bolt_port
        self.http_port = http_port
        self.config = dict(self.config or {})
        self.config["dbms.connector.bolt.advertised_address"] = "localhost:{}".format(self.bolt_port)
        if config:
            self.config.update(**config)

    def __hash__(self):
        return hash(self.fq_name)

    @property
    def fq_name(self):
        return "{}.{}".format(self.name, self.service_name)

    @property
    def discovery_address(self):
        return "{}.{}:5000".format(self.name, self.service_name)

    @property
    def http_uri(self):
        return "http://localhost:{}".format(self.http_port)

    @property
    def bolt_address(self):
        return Address(("localhost", self.bolt_port))


class Neo4jCoreMachineSpec(Neo4jMachineSpec):

    def __init__(self, name, service_name, bolt_port, http_port, config):
        config = config or {}
        config["dbms.mode"] = "CORE"
        super().__init__(name, service_name, bolt_port, http_port, config)


class Neo4jReplicaMachineSpec(Neo4jMachineSpec):

    def __init__(self, name, service_name, bolt_port, http_port, config):
        config = config or {}
        config["dbms.mode"] = "READ_REPLICA"
        super().__init__(name, service_name, bolt_port, http_port, config)


class Neo4jMachine:
    """ A single Neo4j server instance, potentially part of a cluster.
    """

    container = None

    ip_address = None

    ready = 0

    def __init__(self, spec, image, auth):
        self.spec = spec
        self.image = image
        self.address = Address(("localhost", self.spec.bolt_port))
        self.addresses = AddressList([("localhost", self.spec.bolt_port)])
        self.auth = auth
        self.docker = DockerClient.from_env(version="auto")
        environment = {}
        if self.auth:
            environment["NEO4J_AUTH"] = "{}/{}".format(self.auth[0], self.auth[1])
        if "enterprise" in image:
            environment["NEO4J_ACCEPT_LICENSE_AGREEMENT"] = "yes"
        for key, value in self.spec.config.items():
            environment["NEO4J_" + key.replace("_", "__").replace(".", "_")] = value
        ports = {
            "7474/tcp": self.spec.http_port,
            "7687/tcp": self.spec.bolt_port,
        }

        def create_container(img):
            return self.docker.containers.create(img,
                                                 detach=True,
                                                 environment=environment,
                                                 hostname=self.spec.fq_name,
                                                 name=self.spec.fq_name,
                                                 network=self.spec.service_name,
                                                 ports=ports)

        try:
            self.container = create_container(self.image)
        except ImageNotFound:
            log.info("Downloading Docker image %r", self.image)
            self.docker.images.pull(self.image)
            self.container = create_container(self.image)

    def __hash__(self):
        return hash(self.container)

    def __repr__(self):
        return "%s(fq_name=%r, image=%r, address=%r)" % (self.__class__.__name__, self.spec.fq_name, self.image, self.addresses)

    def start(self):
        log.info("Starting machine %r at «%s»", self.spec.fq_name, self.addresses)
        self.container.start()
        self.container.reload()
        self.ip_address = self.container.attrs["NetworkSettings"]["Networks"][self.spec.service_name]["IPAddress"]

    def ping(self, timeout):
        Connection.open(*self.addresses, auth=self.auth, timeout=timeout).close()

    def await_started(self, timeout):
        sleep(1)
        self.container.reload()
        if self.container.status == "running":
            try:
                self.ping(timeout)
            except OSError:
                self.container.reload()
                state = self.container.attrs["State"]
                if state["Status"] == "exited":
                    self.ready = -1
                    log.error("Machine %r exited with code %r", self.spec.fq_name, state["ExitCode"])
                    for line in self.container.logs().splitlines():
                        log.error("> %s" % line.decode("utf-8"))
                else:
                    log.error("Machine %r did not become available within %rs", self.spec.fq_name, timeout)
            else:
                self.ready = 1
        else:
            log.error("Machine %r is not running (status=%r)", self.spec.fq_name, self.container.status)
            for line in self.container.logs().splitlines():
                log.error("> %s" % line.decode("utf-8"))

    def stop(self):
        log.info("Stopping machine %r", self.spec.fq_name)
        self.container.stop()
        self.container.remove(force=True)


class Neo4jService:
    """ A Neo4j database management service.
    """

    default_image = NotImplemented

    default_bolt_port = 7687
    default_http_port = 7474

    snapshot_host = "live.neo4j-build.io"
    snapshot_build_config_id = "Neo4j40_Docker"
    snapshot_build_url = ("https://{}/repository/download/{}/"
                          "lastSuccessful".format(snapshot_host,
                                                  snapshot_build_config_id))

    console_read = None
    console_write = None
    console_args = None
    console_index = None

    def __new__(cls, name=None, image=None, auth=None,
                n_cores=None, n_replicas=None,
                bolt_port=None, http_port=None,
                config=None):
        if n_cores:
            return object.__new__(Neo4jClusterService)
        else:
            return object.__new__(Neo4jStandaloneService)

    def __init__(self, name=None, image=None, auth=None,
                 n_cores=None, n_replicas=None,
                 bolt_port=None, http_port=None,
                 config=None):
        self.name = name or uuid4().hex[-7:]
        self.docker = DockerClient.from_env(version="auto")
        self.image = resolve_image(image or self.default_image)
        self.auth = auth or make_auth()
        if self.auth.user != "neo4j":
            raise ValueError("Auth user must be 'neo4j' or empty")
        self.machines = {}
        self.network = None
        self.console_index = {}
        self._routers = None
        self._readers = None
        self._writers = None
        self._ttl = None

    def __enter__(self):
        try:
            self.start(timeout=300)
        except KeyboardInterrupt:
            self.stop()
            raise
        else:
            return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def _get_machine_by_address(self, address):
        address = Address((address.host, address.port_number))
        for spec, machine in self.machines.items():
            if spec.bolt_address == address:
                return machine

    @property
    def routers(self):
        if self._routers:
            return list(self._routers)
        else:
            return list(self.machines.values())

    @property
    def readers(self):
        return list(self._readers)

    @property
    def writers(self):
        return list(self._writers)

    def _for_each_machine(self, f):
        threads = []
        for spec, machine in self.machines.items():
            thread = Thread(target=f(machine))
            thread.daemon = True
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()

    def start(self, timeout=None):
        log.info("Starting service %r with image %r", self.name, self.image)
        self.network = self.docker.networks.create(self.name)
        self._for_each_machine(lambda machine: machine.start)
        if timeout is not None:
            self.await_started(timeout)

    def await_started(self, timeout):

        def wait(machine):
            machine.await_started(timeout=timeout)

        self._for_each_machine(wait)
        if all(machine.ready == 1 for spec, machine in self.machines.items()):
            log.info("Service %r available", self.name)
        else:
            log.error("Service %r unavailable - some machines failed", self.name)
            raise OSError("Some machines failed")

    def stop(self):
        log.info("Stopping service %r", self.name)
        self._for_each_machine(lambda machine: machine.stop)
        self.network.remove()

    @property
    def addresses(self):
        return AddressList(chain(*(r.addresses for r in self.routers)))

    @classmethod
    def find_and_stop(cls, service_name):
        docker = DockerClient.from_env(version="auto")
        for container in docker.containers.list(all=True):
            if container.name.endswith(".{}".format(service_name)):
                container.stop()
                container.remove(force=True)
        docker.networks.get(service_name).remove()

    def _update_routing_info(self):
        with Connection.open(*self.addresses, auth=self.auth) as cx:
            records = []
            cx.run("CALL dbms.cluster.routing.getRoutingTable($context)",
                   {"context": {}})
            cx.pull(-1, -1, records)
            cx.send_all()
            cx.fetch_all()
            if not records:
                raise RuntimeError("Unable to obtain routing information")
            ttl, server_lists = records[0]
            routers = AddressList()
            readers = AddressList()
            writers = AddressList()
            for server_list in server_lists:
                role = server_list["role"]
                addresses = map(Address.parse, server_list["addresses"])
                if role == "ROUTE":
                    routers[:] = addresses
                elif role == "READ":
                    readers[:] = addresses
                elif role == "WRITE":
                    writers[:] = addresses
            self._routers = [self._get_machine_by_address(a) for a in routers]
            self._readers = [self._get_machine_by_address(a) for a in readers]
            self._writers = [self._get_machine_by_address(a) for a in writers]
            self._ttl = ttl

    def _update_console_index(self):
        self.console_index.update({
            "env": self.console_env,
            "exit": self.console_exit,
            "help": self.console_help,
            "logs": self.console_logs,
            "ls": self.console_list,
            "ping": self.console_ping,
            "rt": self.console_routing,
        })

    def console(self, read, write):
        self.console_read = read
        self.console_write = write
        self._update_console_index()
        self.console_env()
        while True:
            self.console_args = shlex_split(self.console_read(self.name))
            try:
                f = self.console_index[self.console_args[0]]
            except KeyError:
                self.console_write("ERROR!")
            else:
                f()

    def env(self):
        addr = AddressList(chain(*(r.addresses for r in self.routers)))
        auth = "{}:{}".format(self.auth.user, self.auth.password)
        return {
            "BOLT_SERVER_ADDR": str(addr),
            "NEO4J_AUTH": auth,
        }

    def console_env(self):
        """ List the environment variables made available by this service.
        """
        for key, value in sorted(self.env().items()):
            self.console_write("%s=%r" % (key, value))

    def console_exit(self):
        """ Shutdown all machines and exit the console.
        """
        raise SystemExit()

    def console_help(self):
        """ Show descriptions of all available console commands.
        """
        self.console_write("Commands:")
        command_width = max(map(len, self.console_index))
        text_width = 73 - command_width
        template = "  {:<%d}   {}" % command_width
        for command, f in sorted(self.console_index.items()):
            text = " ".join(line.strip() for line in f.__doc__.splitlines())
            wrapped_text = wrap(text, text_width)
            for i, line in enumerate(wrapped_text):
                if i == 0:
                    self.console_write(template.format(command, line))
                else:
                    self.console_write(template.format("", line))

    def console_list(self):
        """ Show a detailed list of the available servers. Each server is
        listed by name, along with the ports open for Bolt and HTTP traffic,
        the mode in which that server is operating -- CORE, READ_REPLICA or
        SINGLE -- the roles it can fulfil -- (r)ead or (w)rite -- and the
        Docker container in which it runs.
        """
        self.console_write("NAME        BOLT PORT   HTTP PORT   "
                           "MODE           ROLES   CONTAINER")
        for spec, machine in self.machines.items():
            if self._routers is None:
                roles = "?"
            else:
                roles = ""
                if machine in self._readers:
                    roles += "r"
                if machine in self._writers:
                    roles += "w"
            self.console_write("{:<12}{:<12}{:<12}{:<15}{:<8}{}".format(
                spec.fq_name,
                spec.bolt_port,
                spec.http_port,
                spec.config.get("dbms.mode", "SINGLE"),
                roles,
                machine.container.short_id,
            ))

    def console_ping(self):
        """ Ping a server by name to check it is available. If no server name
        is provided, 'a' is used as a default.
        """
        try:
            name = self.console_args[1]
        except IndexError:
            name = "a"
        found = 0
        for spec, machine in list(self.machines.items()):
            if name in (spec.name, spec.fq_name):
                machine.ping(timeout=0)
                found += 1
        if not found:
            self.console_write("Machine {} not found".format(name))

    def console_routing(self):
        """ Fetch an updated routing table and display the contents. The
        routing information is cached so that any subsequent `ls` can show
        role information along with each server.
        """
        try:
            self._update_routing_info()
        except RuntimeError:
            self.console_write("Cannot obtain routing information")
        else:
            self.console_write("Routers: «%s»" % AddressList(m.address for m in self._routers))
            self.console_write("Readers: «%s»" % AddressList(m.address for m in self._readers))
            self.console_write("Writers: «%s»" % AddressList(m.address for m in self._writers))
            self.console_write("(TTL: %rs)" % self._ttl)

    def console_logs(self):
        """ Display logs for a named server. If no server name is provided,
        'a' is used as a default.
        """
        try:
            name = self.console_args[1]
        except IndexError:
            name = "a"
        found = 0
        for spec, machine in list(self.machines.items()):
            if name in (spec.name, spec.fq_name):
                self.console_write(machine.container.logs())
                found += 1
        if not found:
            self.console_write("Machine {} not found".format(name))


class Neo4jStandaloneService(Neo4jService):

    default_image = "neo4j:latest"

    def __init__(self, name=None, image=None, auth=None,
                 n_cores=None, n_replicas=None,
                 bolt_port=None, http_port=None,
                 config=None):
        super().__init__(name, image, auth,
                         n_cores, n_replicas,
                         bolt_port, http_port)
        spec = Neo4jMachineSpec(
            name="a",
            service_name=self.name,
            bolt_port=bolt_port or self.default_bolt_port,
            http_port=http_port or self.default_http_port,
            config=config,
        )
        self.machines[spec] = Neo4jMachine(
            spec,
            self.image,
            auth=self.auth,
        )


class Neo4jClusterService(Neo4jService):

    default_image = "neo4j:enterprise"

    # The minimum and maximum number of cores permitted
    min_cores = 3
    max_cores = 7

    # The minimum and maximum number of read replicas permitted
    min_replicas = 0
    max_replicas = 10

    default_bolt_port = 17601
    default_http_port = 17401

    @classmethod
    def _port_range(cls, base_port, count):
        return range(base_port, base_port + count)

    def __init__(self, name=None, image=None, auth=None,
                 n_cores=None, n_replicas=None,
                 bolt_port=None, http_port=None,
                 config=None):
        super().__init__(name, image, auth,
                         n_cores, n_replicas,
                         bolt_port, http_port,
                         config)
        n_cores = n_cores or self.min_cores
        n_replicas = n_replicas or self.min_replicas
        if not self.min_cores <= n_cores <= self.max_cores:
            raise ValueError("A cluster must have been {} and {} cores".format(self.min_cores, self.max_cores))
        if not self.min_replicas <= n_replicas <= self.max_replicas:
            raise ValueError("A cluster must have been {} and {} read replicas".format(self.min_replicas, self.max_replicas))

        core_bolt_port_range = self._port_range(bolt_port or self.default_bolt_port, self.max_cores)
        core_http_port_range = self._port_range(http_port or self.default_http_port, self.max_cores)
        self.free_core_machine_specs = [
            Neo4jCoreMachineSpec(
                name=chr(97 + i),
                service_name=self.name,
                bolt_port=core_bolt_port_range[i],
                http_port=core_http_port_range[i],
                config=dict(config or {}, **{
                    "causal_clustering.minimum_core_cluster_size_at_formation": n_cores or self.min_cores,
                    "causal_clustering.minimum_core_cluster_size_at_runtime": self.min_cores,
                }),
            )
            for i in range(self.max_cores)
        ]
        replica_bolt_port_range = self._port_range(ceil(core_bolt_port_range.stop / 10) * 10, self.max_replicas)
        replica_http_port_range = self._port_range(ceil(core_http_port_range.stop / 10) * 10, self.max_replicas)
        self.free_replica_machine_specs = [
            Neo4jReplicaMachineSpec(
                name=chr(48 + i),
                service_name=self.name,
                bolt_port=replica_bolt_port_range[i],
                http_port=replica_http_port_range[i],
                config=config,
            )
            for i in range(self.max_replicas)
        ]

        # Add core machine specs
        for i in range(n_cores or self.min_cores):
            spec = self.free_core_machine_specs.pop(0)
            self.machines[spec] = None
        # Add replica machine specs
        for i in range(n_replicas or self.min_replicas):
            spec = self.free_replica_machine_specs.pop(0)
            self.machines[spec] = None

        self._boot_machines()

    def _boot_machines(self):
        discovery_addresses = [spec.discovery_address for spec in self.machines
                               if isinstance(spec, Neo4jCoreMachineSpec)]
        for spec, machine in self.machines.items():
            if machine is None:
                spec.config.update({
                    "causal_clustering.initial_discovery_members": ",".join(discovery_addresses),
                })
                self.machines[spec] = Neo4jMachine(spec, self.image, self.auth)

    @property
    def cores(self):
        return [machine for spec, machine in self.machines.items()
                if isinstance(spec, Neo4jCoreMachineSpec)]

    @property
    def replicas(self):
        return [machine for spec, machine in self.machines.items()
                if isinstance(spec, Neo4jReplicaMachineSpec)]

    @property
    def routers(self):
        return list(self.cores)

    def _update_console_index(self):
        super()._update_console_index()
        self.console_index.update({
            "add-core": self.console_add_core,
            "add-replica": self.console_add_replica,
            "rm": self.console_remove,
        })

    def console_add_core(self):
        """ Add new core server
        """
        if len(self.cores) < self.max_cores:
            spec = self.free_core_machine_specs.pop(0)
            self.machines[spec] = None
            self._boot_machines()
            self.machines[spec].start()
            self.machines[spec].await_started(300)
            self.console_write("Added core server %r" % spec.fq_name)
        else:
            self.console_write("A maximum of {} cores "
                               "is permitted".format(self.max_cores))

    def console_add_replica(self):
        """ Add new replica server
        """
        if len(self.replicas) < self.max_replicas:
            spec = self.free_replica_machine_specs.pop(0)
            self.machines[spec] = None
            self._boot_machines()
            self.machines[spec].start()
            self.machines[spec].await_started(300)
            self.console_write("Added replica server %r" % spec.fq_name)
        else:
            self.console_write("A maximum of {} replicas "
                               "is permitted".format(self.max_replicas))

    def _stop_machine(self, spec):
        machine = self.machines[spec]
        del self.machines[spec]
        machine.stop()
        if isinstance(spec, Neo4jCoreMachineSpec):
            self.free_core_machine_specs.append(spec)
        elif isinstance(spec, Neo4jReplicaMachineSpec):
            self.free_replica_machine_specs.append(spec)

    def console_remove(self):
        """ Remove a server by name or role. Servers can be identified either
        by their name (e.g. 'a', 'a.fbe340d') or by the role they fulfil
        (e.g. 'r').
        """
        name = self.console_args[1]
        found = 0
        for spec, machine in list(self.machines.items()):
            if (name == "r" and self._readers is not None and
                    machine in self._readers):
                self._stop_machine(spec)
                found += 1
            elif (name == "w" and self._writers is not None and
                  machine in self._writers):
                self._stop_machine(spec)
                found += 1
            elif name in (spec.name, spec.fq_name):
                self._stop_machine(spec)
                found += 1
        if not found:
            self.console_write("Machine {} not found".format(name))
