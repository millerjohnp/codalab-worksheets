import logging
import os
import psutil
from subprocess import PIPE, Popen
import threading
import time
import socket

import docker
import codalab.worker.docker_utils as docker_utils

from codalab.worker.state_committer import JsonStateCommitter
from codalab.worker.run_manager import BaseRunManager
from codalab.worker.bundle_state import BundleInfo, RunResources, WorkerRun
from .local_run_state import LocalRunStateMachine, LocalRunStage, LocalRunState
from .local_reader import LocalReader

logger = logging.getLogger(__name__)


class LocalRunManager(BaseRunManager):
    """
    LocalRunManager executes the runs locally, each one in its own Docker
    container. It manages its cache of local Docker images and its own local
    Docker network.
    """

    # Network buffer size to use while proxying with netcat
    NETCAT_BUFFER_SIZE = 4096
    # Number of seconds to wait for bundle kills to propagate before forcing kill
    KILL_TIMEOUT = 100
    # Directory name to store running bundles in worker filesystem
    BUNDLES_DIR_NAME = 'runs'
    # Number of loops to check for bundle directory creation by server on shared FS workers
    BUNDLE_DIR_WAIT_NUM_TRIES = 120

    def __init__(
        self,
        worker,  # type: Worker
        image_manager,  # type: DockerImageManager
        dependency_manager,  # type: LocalFileSystemDependencyManager
        commit_file,  # type: str
        cpuset,  # type: Set[str]
        gpuset,  # type: Set[str]
        work_dir,  # type: str
        shared_file_system=False,  # type: bool
        docker_runtime=docker_utils.DEFAULT_RUNTIME,  # type: str
        docker_network_prefix='codalab_worker_network',  # type: str
    ):
        self._worker = worker
        self._state_committer = JsonStateCommitter(commit_file)
        self._reader = LocalReader()
        self._docker = docker.from_env()
        self._shared_file_system = shared_file_system
        if not shared_file_system:
            self._bundles_dir = os.path.join(work_dir, LocalRunManager.BUNDLES_DIR_NAME)
            if not os.path.exists(self._bundles_dir):
                logger.info('%s doesn\'t exist, creating.', self._bundles_dir)
                os.makedirs(self._bundles_dir, 0o770)

        self._image_manager = image_manager
        self._dependency_manager = dependency_manager
        self._cpuset = cpuset
        self._gpuset = gpuset
        self._stop = False
        self._work_dir = work_dir

        self._runs = {}  # bundle_uuid -> LocalRunState
        self._lock = threading.RLock()
        self._init_docker_networks(docker_network_prefix)
        self._run_state_manager = LocalRunStateMachine(
            docker_image_manager=self._image_manager,
            dependency_manager=self._dependency_manager,
            worker_docker_network=self.worker_docker_network,
            docker_network_internal=self.docker_network_internal,
            docker_network_external=self.docker_network_external,
            docker_runtime=docker_runtime,
            upload_bundle_callback=self._worker.upload_bundle_contents,
            assign_cpu_and_gpu_sets_fn=self.assign_cpu_and_gpu_sets,
            shared_file_system=shared_file_system,
        )

    def _init_docker_networks(self, docker_network_prefix):
        """
        Set up docker networks for runs: one with external network access and one without
        """

        def create_or_get_network(name, internal):
            try:
                logger.debug('Creating docker network %s', name)
                return self._docker.networks.create(name, internal=internal, check_duplicate=True)
            except docker.errors.APIError:
                logger.debug('Network %s already exists, reusing', name)
                return self._docker.networks.list(names=[name])[0]

        # Right now the suffix to the general worker network is hardcoded to manually match the suffix
        # in the docker-compose file, so make sure any changes here are synced to there.
        self.worker_docker_network = create_or_get_network(docker_network_prefix + "_general", True)
        self.docker_network_external = create_or_get_network(docker_network_prefix + "_ext", False)
        self.docker_network_internal = create_or_get_network(docker_network_prefix + "_int", True)

    def save_state(self):
        # Remove complex container objects from state before serializing, these can be retrieved
        runs = {
            uuid: state._replace(
                container=None, bundle=state.bundle.to_dict(), resources=state.resources.to_dict()
            )
            for uuid, state in self._runs.items()
        }
        self._state_committer.commit(runs)

    def load_state(self):
        runs = self._state_committer.load()
        # Retrieve the complex container objects from the Docker API
        for uuid, run_state in runs.items():
            if run_state.container_id:
                try:
                    run_state = run_state._replace(
                        container=self._docker.containers.get(run_state.container_id)
                    )
                except docker.errors.NotFound as ex:
                    logger.debug('Error getting the container for the run: %s', ex)
                    run_state = run_state._replace(container_id=None)
            self._runs[uuid] = run_state._replace(
                bundle=BundleInfo.from_dict(run_state.bundle),
                resources=RunResources.from_dict(run_state.resources),
            )

    def start(self):
        """
        Load your state from disk, and start your sub-managers
        """
        self.load_state()
        self._image_manager.start()
        if not self._shared_file_system:
            self._dependency_manager.start()

    def stop(self):
        """
        Starts any necessary cleanup and propagates to its other managers
        Blocks until cleanup is complete and it is safe to quit
        """
        logger.info("Stopping Local Run Manager")
        self._stop = True
        self._image_manager.stop()
        if not self._shared_file_system:
            self._dependency_manager.stop()
        self._run_state_manager.stop()
        self.save_state()
        try:
            self.worker_docker_network.remove()
            self.docker_network_internal.remove()
            self.docker_network_external.remove()
        except docker.errors.APIError as e:
            logger.error("Cannot clear docker networks: {}".format(str(e)))

        logger.info("Stopped Local Run Manager. Exiting")

    def kill_all(self):
        """
        Kills all runs
        """
        logger.debug("Killing all bundles")
        # Set all bundle statuses to killed
        with self._lock:
            for uuid in self._runs.keys():
                run_state = self._runs[uuid]
                run_state = run_state._replace(kill_message='Worker stopped', is_killed=True)
                self._runs[uuid] = run_state
        # Wait until all runs finished or KILL_TIMEOUT seconds pas
        for attempt in range(LocalRunManager.KILL_TIMEOUT):
            with self._lock:
                self._runs = {
                    k: v for k, v in self._runs.items() if v.stage != LocalRunStage.FINISHED
                }
                if len(self._runs) > 0:
                    logger.debug(
                        "Waiting for {} more bundles. {} seconds until force quit.".format(
                            len(self._runs), LocalRunManager.KILL_TIMEOUT - attempt
                        )
                    )
            time.sleep(1)

    def process_runs(self):
        """ Transition each run then filter out finished runs """
        with self._lock:
            # transition all runs
            for bundle_uuid in self._runs.keys():
                run_state = self._runs[bundle_uuid]
                self._runs[bundle_uuid] = self._run_state_manager.transition(run_state)

            # filter out finished runs
            finished_container_ids = [
                run.container
                for run in self._runs.values()
                if (run.stage == LocalRunStage.FINISHED or run.stage == LocalRunStage.FINALIZING)
                and run.container_id is not None
            ]
            for container_id in finished_container_ids:
                try:
                    container = self._docker.containers.get(container_id)
                    container.remove(force=True)
                except (docker.errors.NotFound, docker.errors.NullResource):
                    pass
            self._runs = {k: v for k, v in self._runs.items() if v.stage != LocalRunStage.FINISHED}

    def create_run(self, bundle, resources):
        """
        Creates and starts processing a new run with the given bundle and
        resources
        """
        if self._stop:
            # Run Manager stopped, refuse more runs
            return
        if self._shared_file_system:
            bundle_path = bundle.location
        else:
            bundle_path = os.path.join(self._bundles_dir, bundle.uuid)
        now = time.time()
        run_state = LocalRunState(
            stage=LocalRunStage.PREPARING,
            run_status='',
            bundle=bundle,
            bundle_path=os.path.realpath(bundle_path),
            bundle_dir_wait_num_tries=LocalRunManager.BUNDLE_DIR_WAIT_NUM_TRIES,
            resources=resources,
            bundle_start_time=now,
            container_start_time=None,
            container_time_total=0,
            container_time_user=0,
            container_time_system=0,
            container_id=None,
            container=None,
            docker_image=None,
            is_killed=False,
            has_contents=False,
            cpuset=None,
            gpuset=None,
            max_memory=0,
            disk_utilization=0,
            exitcode=None,
            failure_message=None,
            kill_message=None,
            finished=False,
            finalized=False,
        )
        with self._lock:
            self._runs[bundle.uuid] = run_state

    def assign_cpu_and_gpu_sets(self, request_cpus, request_gpus):
        """
        Propose a cpuset and gpuset to a bundle based on given requested resources.
        Note: no side effects (this is important: we don't want to maintain more state than necessary)

        Arguments:
            request_cpus: integer
            request_gpus: integer

        Returns a 2-tuple:
            cpuset: assigned cpuset (str indices).
            gpuset: assigned gpuset (str indices).

        Throws an exception if unsuccessful.
        """
        cpuset, gpuset = set(self._cpuset), set(self._gpuset)

        with self._lock:
            for run_state in self._runs.values():
                if run_state.stage == LocalRunStage.RUNNING:
                    cpuset -= run_state.cpuset
                    gpuset -= run_state.gpuset

        if len(cpuset) < request_cpus:
            raise Exception(
                "Requested more CPUs (%d) than available (%d currently out of %d on the machine)"
                % (request_cpus, len(cpuset), len(self._cpuset))
            )
        if len(gpuset) < request_gpus:
            raise Exception(
                "Requested more GPUs (%d) than available (%d currently out of %d on the machine)"
                % (request_gpus, len(gpuset), len(self._gpuset))
            )

        def propose_set(resource_set, request_count):
            return set(str(el) for el in list(resource_set)[:request_count])

        return propose_set(cpuset, request_cpus), propose_set(gpuset, request_gpus)

    def has_run(self, uuid):
        """
        Returns True if the run with the given UUID is managed
        by this RunManager, False otherwise
        """
        with self._lock:
            return uuid in self._runs

    def mark_finalized(self, uuid):
        """
        Marks the run as finalized server-side so it can be discarded
        """
        if uuid in self._runs:
            with self._lock:
                self._runs[uuid] = self._runs[uuid]._replace(finalized=True)

    def read(self, uuid, path, args, reply):
        """
        Use your Reader helper to invoke the given read command
        """
        run_state = self._runs[uuid]
        self._reader.read(run_state, path, args, reply)

    def write(self, uuid, path, string):
        """
        Write `string` (string) to path in bundle with uuid.
        """
        run_state = self._runs[uuid]
        if os.path.normpath(path) in set(dep.child_path for dep in run_state.bundle.dependencies):
            return
        with open(os.path.join(run_state.bundle_path, path), 'w') as f:
            f.write(string)

    def netcat(self, uuid, port, message, reply):
        """
        Write `message` (string) to port of bundle with uuid and read the response.
        Returns a stream with the response contents (bytes).
        """
        # TODO: handle this in a thread since this could take a while
        run_state = self._runs[uuid]
        container_ip = docker_utils.get_container_ip(
            self.worker_docker_network.name, run_state.container
        )
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((container_ip, port))
        s.sendall(message.encode())

        total_data = []
        while True:
            data = s.recv(LocalRunManager.NETCAT_BUFFER_SIZE)
            if not data:
                break
            total_data.append(data)
        s.close()
        reply(None, {}, b''.join(total_data))

    def kill(self, uuid):
        """
        Kill bundle with uuid
        """
        run_state = self._runs[uuid]
        with self._lock:
            run_state = run_state._replace(kill_message='Kill requested', is_killed=True)
            self._runs[run_state.bundle.uuid] = run_state

    @property
    def all_runs(self):
        """
        Returns a list of all the runs managed by this RunManager
        """
        with self._lock:
            return [
                WorkerRun(
                    uuid=run_state.bundle.uuid,
                    run_status=run_state.run_status,
                    bundle_start_time=run_state.bundle_start_time,
                    container_start_time=run_state.container_start_time,
                    container_time_total=run_state.container_time_total,
                    container_time_user=run_state.container_time_user,
                    container_time_system=run_state.container_time_system,
                    docker_image=run_state.docker_image,
                    state=LocalRunStage.WORKER_STATE_TO_SERVER_STATE[run_state.stage],
                    remote=self._worker.id,
                    exitcode=run_state.exitcode,
                    failure_message=run_state.failure_message,
                )
                for run_state in self._runs.values()
            ]

    @property
    def all_dependencies(self):
        """
        Returns a list of all dependencies available in this RunManager
        If on a shared filesystem, reports nothing since all bundles are on the
        same filesystem and the concept of caching dependencies doesn't apply
        to this worker.
        """
        if self._shared_file_system:
            return []
        return self._dependency_manager.all_dependencies

    @property
    def cpus(self):
        """
        Total number of CPUs this RunManager has
        """
        return len(self._cpuset)

    @property
    def gpus(self):
        """
        Total number of GPUs this RunManager has
        """
        return len(self._gpuset)

    @property
    def memory_bytes(self):
        """
        Total installed memory of this RunManager
        """
        return psutil.virtual_memory().total

    @property
    def free_disk_bytes(self):
        """
        Available disk space by bytes of this RunManager.
        """
        error_msg = "Failed to run command {}".format("df " + self._work_dir)
        try:
            p = Popen(["df", self._work_dir], stdout=PIPE)
            output, error = p.communicate()
            # Return None when there is an error.
            if error:
                logger.error(error.strip())
                return None

            if output:
                lines = output.decode().split("\n")
                index = lines[0].split().index("Available")
                # We convert the original result from df command in unit of 1KB blocks into bytes.
                return int(lines[1].split()[index]) * 1024

        except Exception as e:
            logger.error("{}: {}".format(error_msg, str(e)))
            return None
