# Copyright (C) 2015-2021 Regents of the University of California
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
import enum
import logging
import os
import shutil
from abc import ABC, abstractmethod
from argparse import ArgumentParser, _ArgumentGroup
from contextlib import contextmanager
from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    TypeVar,
    Union,
)

from toil.bus import MessageBus
from toil.common import Config, Toil, cacheDirName
from toil.deferred import DeferredFunctionManager
from toil.fileStores.abstractFileStore import AbstractFileStore
from toil.job import JobDescription, Requirer, ParsedRequirement
from toil.resource import Resource

logger = logging.getLogger(__name__)

# Value to use as exitStatus in UpdatedBatchJobInfo.exitStatus when status is not available.
EXIT_STATUS_UNAVAILABLE_VALUE = 255

class BatchJobExitReason(enum.Enum):
    FINISHED: int = 1  # Successfully finished.
    FAILED: int = 2  # Job finished, but failed.
    LOST: int = 3  # Preemptable failure (job's executing host went away).
    KILLED: int = 4  # Job killed before finishing.
    ERROR: int = 5  # Internal error.
    MEMLIMIT: int = 6  # Job hit batch system imposed memory limit

class UpdatedBatchJobInfo(NamedTuple):
    jobID: int
    exitStatus: int
    """
    The exit status (integer value) of the job. 0 implies successful.

    EXIT_STATUS_UNAVAILABLE_VALUE is used when the exit status is not available (e.g. job is lost).
    """

    exitReason: Optional[BatchJobExitReason]
    wallTime: Union[float, int, None]

# Information required for worker cleanup on shutdown of the batch system.
class WorkerCleanupInfo(NamedTuple):
    work_dir: Optional[str]
    """Work directory path (where the cache would go) if specified by user"""

    coordination_dir: Optional[str]
    """Coordination directory path (where lock files would go) if specified by user"""

    workflow_id: str
    """Used to identify files specific to this workflow"""

    clean_work_dir: str
    """
    When to clean up the work and coordination directories for a job ('always',
    'onSuccess', 'onError', 'never')
    """


class AbstractBatchSystem(ABC):
    """An abstract base class to represent the interface the batch system must provide to Toil."""
    @classmethod
    @abstractmethod
    def supportsAutoDeployment(cls) -> bool:
        """
        Whether this batch system supports auto-deployment of the user script itself.

        If it does, the :meth:`.setUserScript` can be invoked to set the resource
        object representing the user script.

        Note to implementors: If your implementation returns True here, it should also override
        """
        raise NotImplementedError()

    @classmethod
    @abstractmethod
    def supportsWorkerCleanup(cls) -> bool:
        """
        Indicates whether this batch system invokes
        :meth:`BatchSystemSupport.workerCleanup` after the last job for a
        particular workflow invocation finishes. Note that the term *worker*
        refers to an entire node, not just a worker process. A worker process
        may run more than one job sequentially, and more than one concurrent
        worker process may exist on a worker node, for the same workflow. The
        batch system is said to *shut down* after the last worker process
        terminates.
        """
        raise NotImplementedError()

    def setUserScript(self, userScript: Resource) -> None:
        """
        Set the user script for this workflow. This method must be called before the first job is
        issued to this batch system, and only if :meth:`.supportsAutoDeployment` returns True,
        otherwise it will raise an exception.

        :param userScript: the resource object representing the user script
               or module and the modules it depends on.
        """
        raise NotImplementedError()

    def set_message_bus(self, message_bus: MessageBus) -> None:
        """
        Give the batch system an opportunity to connect directly to the message
        bus, so that it can send informational messages about the jobs it is
        running to other Toil components.

        Currently the only message a batch system may send is
        JobAnnotationMessage.
        """
        # By default, do nothing.
        pass

    @abstractmethod
    def issueBatchJob(self, jobDesc: JobDescription, job_environment: Optional[Dict[str, str]] = None) -> int:
        """
        Issues a job with the specified command to the batch system and returns
        a unique jobID.

        :param jobDesc: a toil.job.JobDescription
        :param job_environment: a collection of job-specific environment
                                variables to be set on the worker.

        :return: a unique jobID that can be used to reference the newly issued
                 job
        """
        raise NotImplementedError()

    @abstractmethod
    def killBatchJobs(self, jobIDs: List[int]) -> None:
        """
        Kills the given job IDs. After returning, the killed jobs will not
        appear in the results of getRunningBatchJobIDs. The killed job will not
        be returned from getUpdatedBatchJob.

        :param jobIDs: list of IDs of jobs to kill
        """
        raise NotImplementedError()

    # FIXME: Return value should be a set (then also fix the tests)

    @abstractmethod
    def getIssuedBatchJobIDs(self) -> List[int]:
        """
        Gets all currently issued jobs

        :return: A list of jobs (as jobIDs) currently issued (may be running, or may be
                 waiting to be run). Despite the result being a list, the ordering should not
                 be depended upon.
        """
        raise NotImplementedError()

    @abstractmethod
    def getRunningBatchJobIDs(self) -> Dict[int, float]:
        """
        Gets a map of jobs as jobIDs that are currently running (not just waiting)
        and how long they have been running, in seconds.

        :return: dictionary with currently running jobID keys and how many seconds they have
                 been running as the value
        """
        raise NotImplementedError()

    @abstractmethod
    def getUpdatedBatchJob(self, maxWait: int) -> Optional[UpdatedBatchJobInfo]:
        """
        Returns information about job that has updated its status (i.e. ceased
        running, either successfully or with an error). Each such job will be
        returned exactly once.

        Does not return info for jobs killed by killBatchJobs, although they
        may cause None to be returned earlier than maxWait.

        :param maxWait: the number of seconds to block, waiting for a result

        :return: If a result is available, returns UpdatedBatchJobInfo.
                 Otherwise it returns None. wallTime is the number of seconds (a strictly
                 positive float) in wall-clock time the job ran for, or None if this
                 batch system does not support tracking wall time.
        """
        raise NotImplementedError()

    def getSchedulingStatusMessage(self) -> Optional[str]:
        """
        Get a log message fragment for the user about anything that might be
        going wrong in the batch system, if available.

        If no useful message is available, return None.

        This can be used to report what resource is the limiting factor when
        scheduling jobs, for example. If the leader thinks the workflow is
        stuck, the message can be displayed to the user to help them diagnose
        why it might be stuck.

        :return: User-directed message about scheduling state.
        """

        # Default implementation returns None.
        # Override to provide scheduling status information.
        return None

    @abstractmethod
    def shutdown(self) -> None:
        """
        Called at the completion of a toil invocation.
        Should cleanly terminate all worker threads.
        """
        raise NotImplementedError()

    def setEnv(self, name: str, value: Optional[str] = None) -> None:
        """
        Set an environment variable for the worker process before it is launched. The worker
        process will typically inherit the environment of the machine it is running on but this
        method makes it possible to override specific variables in that inherited environment
        before the worker is launched. Note that this mechanism is different to the one used by
        the worker internally to set up the environment of a job. A call to this method affects
        all jobs issued after this method returns. Note to implementors: This means that you
        would typically need to copy the variables before enqueuing a job.

        If no value is provided it will be looked up from the current environment.
        """
        raise NotImplementedError()

    @classmethod
    def add_options(cls, parser: Union[ArgumentParser, _ArgumentGroup]) -> None:
        """
        If this batch system provides any command line options, add them to the given parser.
        """

    OptionType = TypeVar('OptionType')
    @classmethod
    def setOptions(cls, setOption: Callable[[str, Optional[Callable[[Any], OptionType]], Optional[Callable[[OptionType], None]], Optional[OptionType], Optional[List[str]]], None]) -> None:
        """
        Process command line or configuration options relevant to this batch system.

        :param setOption: A function with signature
            setOption(option_name, parsing_function=None, check_function=None, default=None, env=None)
            returning nothing, used to update run configuration as a side effect.
        """
        # TODO: change type to a Protocol to express kwarg names, or else use a
        # different interface (generator?)

    def getWorkerContexts(self) -> List[ContextManager[Any]]:
        """
        Get a list of picklable context manager objects to wrap worker work in,
        in order.

        Can be used to ask the Toil worker to do things in-process (such as
        configuring environment variables, hot-deploying user scripts, or
        cleaning up a node) that would otherwise require a wrapping "executor"
        process.
        """
        return []


class BatchSystemSupport(AbstractBatchSystem):
    """Partial implementation of AbstractBatchSystem, support methods."""

    def __init__(self, config: Config, maxCores: float, maxMemory: int, maxDisk: int) -> None:
        """
        Initialize initial state of the object.

        :param toil.common.Config config: object is setup by the toilSetup script and
          has configuration parameters for the jobtree. You can add code
          to that script to get parameters for your batch system.

        :param float maxCores: the maximum number of cores the batch system can
          request for any one job

        :param int maxMemory: the maximum amount of memory the batch system can
          request for any one job, in bytes

        :param int maxDisk: the maximum amount of disk space the batch system can
          request for any one job, in bytes
        """
        super().__init__()
        self.config = config
        self.maxCores = maxCores
        self.maxMemory = maxMemory
        self.maxDisk = maxDisk
        self.environment: Dict[str, str] = {}
        if config.workflowID is None:
            raise Exception("config.workflowID must be set")
        else:
            self.workerCleanupInfo = WorkerCleanupInfo(
                work_dir=config.workDir,
                coordination_dir=config.coordination_dir,
                workflow_id=config.workflowID,
                clean_work_dir=config.cleanWorkDir,
            )

    def check_resource_request(self, requirer: Requirer) -> None:
        """
        Check resource request is not greater than that available or allowed.

        :param requirer: Object whose requirements are being checked

        :param str job_name: Name of the job being checked, for generating a useful error report.

        :param str detail: Batch-system-specific message to include in the error.

        :raise InsufficientSystemResources: raised when a resource is requested in an amount
               greater than allowed
        """
        try:
            for resource, requested, available in [('cores', requirer.cores, self.maxCores),
                                                   ('memory', requirer.memory, self.maxMemory),
                                                   ('disk', requirer.disk, self.maxDisk)]:
                assert requested is not None
                if requested > available:
                    raise InsufficientSystemResources(requirer, resource, requested, available)
            # Handle accelerators in another method that can be overridden separately
            self._check_accelerator_request(requirer)
        except InsufficientSystemResources as e:
            # Add more annotation info to the error
            e.batch_system = self.__class__.__name__ or None
            e.source = self.config.workDir if e.resource == 'disk' else None
            raise e
            
    def _check_accelerator_request(self, requirer: Requirer) -> None:
        """
        Raise an InsufficientSystemResources error if the batch system can't
        provide the accelerators that are required.
        
        If a batch system *can* provide accelerators, it should override this
        to say so.
        """
        if len(requierer.accelerators) > 0:
            # By default we assume we can't fulfil any of these
            raise InsufficientSystemResources(requirer, 'accelerators', requierer.accelerators, [])
    
    def setEnv(self, name: str, value: Optional[str] = None) -> None:
        """
        Set an environment variable for the worker process before it is launched. The worker
        process will typically inherit the environment of the machine it is running on but this
        method makes it possible to override specific variables in that inherited environment
        before the worker is launched. Note that this mechanism is different to the one used by
        the worker internally to set up the environment of a job. A call to this method affects
        all jobs issued after this method returns. Note to implementors: This means that you
        would typically need to copy the variables before enqueuing a job.

        If no value is provided it will be looked up from the current environment.

        :param str name: the environment variable to be set on the worker.

        :param str value: if given, the environment variable given by name will be set to this value.
               if None, the variable's current value will be used as the value on the worker

        :raise RuntimeError: if value is None and the name cannot be found in the environment
        """
        if value is None:
            try:
                value = os.environ[name]
            except KeyError:
                raise RuntimeError(f"{name} does not exist in current environment")
        self.environment[name] = value

    def formatStdOutErrPath(self, toil_job_id: int, cluster_job_id: str, std: str) -> str:
        """
        Format path for batch system standard output/error and other files
        generated by the batch system itself.

        Files will be written to the Toil work directory (which may
        be on a shared file system) with names containing both the Toil and
        batch system job IDs, for ease of debugging job failures.

        :param: int toil_job_id : The unique id that Toil gives a job.
        :param: cluster_job_id : What the cluster, for example, GridEngine, uses as its internal job id.
        :param: string std : The provenance of the stream (for example: 'err' for 'stderr' or 'out' for 'stdout')

        :rtype: string : Formatted filename; however if self.config.noStdOutErr is true,
             returns '/dev/null' or equivalent.
        """
        if self.config.noStdOutErr:
            return os.devnull

        fileName: str = f'toil_{self.config.workflowID}.{toil_job_id}.{cluster_job_id}.{std}.log'
        workDir: str = Toil.getToilWorkDir(self.config.workDir)
        return os.path.join(workDir, fileName)

    @staticmethod
    def workerCleanup(info: WorkerCleanupInfo) -> None:
        """
        Cleans up the worker node on batch system shutdown. Also see :meth:`supportsWorkerCleanup`.

        :param WorkerCleanupInfo info: A named tuple consisting of all the relevant information
               for cleaning up the worker.
        """
        assert isinstance(info, WorkerCleanupInfo)
        assert info.workflow_id is not None
        workflowDir = Toil.getLocalWorkflowDir(info.workflow_id, info.work_dir)
        coordination_dir = Toil.get_local_workflow_coordination_dir(info.workflow_id, info.work_dir, info.coordination_dir)
        DeferredFunctionManager.cleanupWorker(coordination_dir)
        workflowDirContents = os.listdir(workflowDir)
        AbstractFileStore.shutdownFileStore(info.workflow_id, info.work_dir, info.coordination_dir)
        if info.clean_work_dir in ('always', 'onSuccess', 'onError'):
            if workflowDirContents in ([], [cacheDirName(info.workflow_id)]):
                shutil.rmtree(workflowDir, ignore_errors=True)
            if coordination_dir != workflowDir:
                # No more coordination to do here either.
                shutil.rmtree(coordination_dir, ignore_errors=True)

class NodeInfo:
    """
    The coresUsed attribute  is a floating point value between 0 (all cores idle) and 1 (all cores
    busy), reflecting the CPU load of the node.

    The memoryUsed attribute is a floating point value between 0 (no memory used) and 1 (all memory
    used), reflecting the memory pressure on the node.

    The coresTotal and memoryTotal attributes are the node's resources, not just the used resources

    The requestedCores and requestedMemory attributes are all the resources that Toil Jobs have reserved on the
    node, regardless of whether the resources are actually being used by the Jobs.

    The workers attribute is an integer reflecting the number of workers currently active workers
    on the node.
    """
    def __init__(self, coresUsed: float, memoryUsed: float,
                 coresTotal: float, memoryTotal: int,
                 requestedCores: float, requestedMemory: int,
                 workers: int) -> None:
        self.coresUsed = coresUsed
        self.memoryUsed = memoryUsed

        self.coresTotal = coresTotal
        self.memoryTotal = memoryTotal

        self.requestedCores = requestedCores
        self.requestedMemory = requestedMemory

        self.workers = workers


class AbstractScalableBatchSystem(AbstractBatchSystem):
    """
    A batch system that supports a variable number of worker nodes. Used by :class:`toil.
    provisioners.clusterScaler.ClusterScaler` to scale the number of worker nodes in the cluster
    up or down depending on overall load.
    """

    @abstractmethod
    def getNodes(self, preemptable: Optional[bool] = None, timeout: int = 600) -> Dict[str, NodeInfo]:
        """
        Returns a dictionary mapping node identifiers of preemptable or non-preemptable nodes to
        NodeInfo objects, one for each node.

        :param preemptable: If True (False) only (non-)preemptable nodes will be returned.
               If None, all nodes will be returned.
        """
        raise NotImplementedError()

    @abstractmethod
    def nodeInUse(self, nodeIP: str) -> bool:
        """
        Can be used to determine if a worker node is running any tasks. If the node is doesn't
        exist, this function should simply return False.

        :param nodeIP: The worker nodes private IP address

        :return: True if the worker node has been issued any tasks, else False
        """
        raise NotImplementedError()

    @abstractmethod
    def ignoreNode(self, nodeAddress: str) -> None:
        """
        Stop sending jobs to this node. Used in autoscaling
        when the autoscaler is ready to terminate a node, but
        jobs are still running. This allows the node to be terminated
        after the current jobs have finished.

        :param nodeAddress: IP address of node to ignore.
        """
        raise NotImplementedError()

    @abstractmethod
    def unignoreNode(self, nodeAddress: str) -> None:
        """
        Stop ignoring this address, presumably after
        a node with this address has been terminated. This allows for the
        possibility of a new node having the same address as a terminated one.
        """
        raise NotImplementedError()


class InsufficientSystemResources(Exception):
    def __init__(self, requirer: Requirer, resource: str, requested: Optional[ParsedRequirement] = None, available: Optional[ParsedRequirement] = None, batch_system: Optional[str] = None, source: Optional[str] = None, details: List[str] = []) -> None:
        """
        Make a new exception about how we couldn't get enough of something.
        
        :param requirer: What needed the resources. May have a .jobName string.
        :param resource: The kind of resource requested (cores, memory, disk, accelerators).
        :param requested: The amount requested.
        :param available: The amount actually available.
        :param batch_system: The batch system that could not provide the resource.
        :param source: The place where the resource was to be gotten from. For disk, should be a path.
        :param details: Any extra details about the problem that can be attached to the error.
        """
        if hasattr(requirer, 'jobName') and isinstance(requirer.jobName, str):
            # Keep the job name if any
            self.job_name = requirer.jobName
        else:
            self.job_name = None
        
        self.resource = resource
        self.requested = requested
        self.available = available
        self.batch_system = batch_system
        self.source = source
        self.details = details
        
    def __str__(self) -> str:
        """
        Explain the exception.
        """
        
        unit = 'bytes of ' if self.resource in ('disk', 'memory') else ''
        purpose = ' for temporary space' if self.resource == 'disk' else ''
        qualifier = ' free on {self.source}' if self.resource == disk and self.source is not None else ''
        
        msg = []
        if self.job_name is not None:
            msg.append(f'The job {job_name} is requesting ')
        else:
            msg.append(f'Requesting ')
        if self.requested is not None:
            msg.append(str(self.requested))
            msg.append(f' {unit}{self.resource}')
        msg.append(purpose)
        if self.available is not None:
            msg.append(f', more than the maximum of {available} {unit}{self.resource}{qualifier} that {self.batch_system or "this batch system"} was configured with')
            if self.resource in ('cores', 'memory', 'disk'):
                msg.append(f', or enforced by --max{resource.capitalize()}')
        else:
            msg.append(', but that is not available')
        msg.append('.')
        
        if self.resource == 'disk':
            msg.append(' Try setting/changing the toil option "--workDir" or changing the base temporary directory by setting TMPDIR.')
        
        for detail in details:
            msg.append(' ')
            msg.append(detail)
            
        return ''.join(msg)

