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

"""
Message types and message bus for leader component coordination.
"""

import collections
import inspect
import logging
from typing import Any, Callable, Dict, IO, Iterator, List, NamedTuple, Optional, Type, TypeVar

from pubsub.core import Publisher
from pubsub.core.listener import Listener

logger = logging.getLogger( __name__ )

class JobIssuedMessage(NamedTuple):
    """
    Produced when a job is issued to run on the batch system.
    """
    # The kind of job issued, for statistics aggregation
    job_type: str
    # The job store ID of the job
    job_id: str

class JobUpdatedMessage(NamedTuple):
    """
    Produced when a job is "updated" and ready to have something happen to it.
    """
    # The job store ID of the job
    job_id: str
    # The error code/return code for the job, which is nonzero if something has
    # gone wrong, and 0 otherwise.
    result_status: int

class JobCompletedMessage(NamedTuple):
    """
    Produced when a job is completed, whether successful or not.
    """
    # The kind of job issued, for statistics aggregation
    job_type: str
    # The job store ID of the job
    job_id: str

class JobFailedMessage(NamedTuple):
    """
    Produced when a job is completely failed, and will not be retried again.
    """
    # The kind of job issued, for statistics aggregation
    job_type: str
    # The job store ID of the job
    job_id: str

class JobMissingMessage(NamedTuple):
    """
    Produced when a job goes missing and should be in the batch system but isn't.
    """
    # The job store ID of the job
    job_id: str
    
class QueueSizeMessage(NamedTuple):
    """
    Produced to describe the size of the queue of jobs issued but not yet
    completed. Theoretically recoverable from other messages.
    """
    # The size of the queue
    queue_size: int

class ClusterSizeMessage(NamedTuple):
    """
    Produced by the Toil-integrated autoscaler describe the number of
    instances of a certain type in a cluster.
    """
    # The instance type name, like t4g.medium
    instance_type: str
    # The number of them that the Toil autoscaler thinks there are
    current_size: int

class ClusterDesiredSizeMessage(NamedTuple):
    """
    Produced by the Toil-integrated autoscaler to describe the number of
    instances of a certain type that it thinks will be needed.
    """
    # The instance type name, like t4g.medium
    instance_type: str
    # The number of them that the Toil autoscaler wants there to be
    desired_size: int

class MessageBus:
    """
    Holds messages that should cause jobs to change their scheduling states.
    Messages are put in and buffered, and can be taken out and handled as
    batches when convenient.

    All messages are NamedTuple objects of various subtypes.

    Message order is guaranteed to be preserved within a type.

    TODO: Not yet thread safe, but should be made thread safe if we want e.g.
    the ServiceManager to talk to it. Note that defaultdict itself isn't
    necessarily thread safe.
    """

    def __init__(self) -> None:
        # Each MessageBus has an independent PyPubSub instance
        self._pubsub = Publisher()
        
    def _type_to_name(self, message_type: type) -> str:
        """
        Convert a type to a name that can be a PyPubSub topic (all normal
        characters, hierarchically dotted).
        """
        
        return '.'.join([message_type.__module__, message_type.__name__])

    # All our messages are NamedTuples, but NamedTuples don't actually inherit
    # from NamedTupe, so MyPy complains if we require that here.
    def publish(self, message: Any) -> None:
        """
        Put a message onto the bus.
        """
        topic = self._type_to_name(type(message))
        logger.debug('Notifying %s with message: %s', topic, message)
        self._pubsub.sendMessage(topic, message=message)

    # This next function takes callables that take things of the type that was passed in as a
    # runtime argument, which we can explain to MyPy using a TypeVar and Type[]
    MessageType = TypeVar('MessageType')
    def subscribe(self, message_type: Type[MessageType], handler: Callable[[MessageType], Any]) -> Listener:
        """
        Register the given callable to be called when messages of the given type are sent.
        It will be called with messages sent after the subscription is created.
        Returns a subscription object; when the subscription object is GC'd the subscription will end.
        """

        topic = self._type_to_name(message_type)
        logger.debug('Listening for message topic: %s', topic)

        # Make sure to wrap the handler so we get the right argument name and
        # we can control lifetime.
        def handler_wraper(message: MessageBus.MessageType) -> None:
            handler(message)

        # The docs says this returns the listener but really it seems to return
        # a listener and something else. 
        listener, _ = self._pubsub.subscribe(handler_wraper, topic)
        # Hide the handler function in the pubsub listener to keep it alive.
        # If it goes out of scope the subscription expires, and the pubsub
        # system only uses weak references.
        setattr(listener, 'handler_wrapper', handler_wraper)
        return listener

    def connect(self, wanted_types: List[type]) -> 'MessageBusConnection':
        """
        Get a connection object that serves as an inbox for messages of the
        given types.
        Messages of those types will accumulate in the inbox until it is
        destroyed. You can check for them at any time.
        """
        connection = MessageBusConnection()
        # We call this private method, really we mean this to be module scope.
        connection._set_bus_and_message_types(self, wanted_types)
        return connection
        
        
    def outbox(self) -> 'MessageOutbox':
        """
        Get a connection object that only allows sending messages.
        """
        connection = MessageOutbox()
        connection._set_bus(self)
        return connection

    @classmethod
    def decode_bus_messages(cls, stream: IO[bytes], message_types: List[Type[MessageType]]) -> 'MessageInbox':
        """
        Get an inbox for all messages in the given log stream. Discard any
        trailing partial messages.
        
        All messages in the stream will be in the inbox by the time it is
        returned; it only needs to be checked once.
        """
        raise NotImplementedError()
        return MessageInbox()

class MessageInbox:
    """
    A buffered connection to a message bus that lets us receive messages.
    Buffers incoming messages until you are ready for them.
    """
    
    def __init__(self) -> None:
        """
        Make a disconnected inbox.
        """
        
        super().__init__()
        
        # This holds all the messages on the bus, organized by type.
        self._messages_by_type: Dict[type, List[Any]] = {}
        # This holds listeners for all the types, when we connect to a bus
        self._listeners_by_type: Dict[type, Listener] = {}
        
        # We define a handler for messages
        def on_message(message: Any) -> None:
            self._messages_by_type[type(message)].append(message)
        self._handler = on_message
        
    def _set_bus_and_message_types(self, bus: MessageBus, wanted_types: List[type]) -> None:
        """
        Connect to the given bus and collect the given message types.
        
        We must not have connected to anything yet.
        """
        
        for t in wanted_types:
            # or every kind of message we are subscribing to
            
            # Make a quue for the messages
            self._messages_by_type[t] = []
            # Make and save a subscription
            self._listeners_by_type[t] = bus.subscribe(t, self._handler)
            
    def count(self, message_type: type) -> int:
        """
        Get the number of pending messages of the given type.
        """

        return len(self._messages_by_type[message_type])

    def empty(self) -> bool:
        """
        Return True if no messages are pending, and false otherwise.
        """

        return all(len(v) == 0 for v in self._messages_by_type.values())

    # This next function returns things of the type that was passed in as a
    # runtime argument, which we can explain to MyPy using a TypeVar and Type[]
    MessageType = TypeVar('MessageType')
    def for_each(self, message_type: Type[MessageType]) -> Iterator[MessageType]:
        """
        Loop over all messages currently pending of the given type. Each that
        is handled without raising an exception will be removed.

        Messages sent while this function is running will not be yielded by the
        current call.
        """

        # Grab the message buffer for this kind of message.
        message_list = self._messages_by_type[message_type]
        # Make a new buffer. TODO: Will be hard to be thread-safe because other
        # threads could have a reference to the old buffer.
        self._messages_by_type[message_type] = []

        # Flip around to chronological order
        message_list.reverse()
        try:
            while len(message_list) > 0:
                # We need to handle the case where a new message of this type comes
                # in while we're looping, from the handler. So we take each off the
                # list from the end while we handle it, and put it back if
                # something goes wrong.
                message = message_list.pop()
                handled = False
                try:
                    # Emit the message
                    assert isinstance(message, message_type), f"Unacceptable message type {type(message)} in list for type {message_type}"
                    yield message
                    # If we get here it was handled without error.
                    handled = True
                finally:
                    if not handled:
                        # An exception happened, and we're bailing out of the
                        # while loop. Make sure the message isn't dropped, in
                        # case someone wants to recover and handle it again
                        # later with another for_each call.
                        message_list.append(message)
        finally:
            # Dump anything remaining in our buffer back into the main buffer,
            # in the right order, and before the later messages.
            message_list.reverse()
            self._messages_by_type[message_type] = message_list + self._messages_by_type[message_type]

class MessageOutbox:
    """
    A connection to a message bus that lets us publish messages.
    """
    
    def __init__(self) -> None:
        """
        Make a disconnected outbox.
        """
        super().__init__()
        self._bus: Optional[MessageBus] = None
    
    def _set_bus(self, bus: MessageBus) -> None:
        """
        Connect to the given bus.
        
        We must not have connected to anything yet.
        """
        self._bus = bus
        
    def publish(self, message: Any) -> None:
        """
        Publish the given message to the connected message bus.

        We have this so you don't need to store both the bus and your connection.
        """
        if self._bus is None:
            raise RuntimeError("Cannot send message when not connected to a bus")
        self._bus.publish(message)
        
class MessageBusConnection(MessageInbox, MessageOutbox):
    """
    A two-way connection to a message bus. Buffers incoming messages until you
    are ready for them, and lets you send messages.
    """

    def __init__(self) -> None:
        """
        Make a MessageBusConnection that is not connected yet.
        """
        super().__init__()
        
    
    def _set_bus_and_message_types(self, bus: MessageBus, wanted_types: List[type]) -> None:
        """
        Connect to the given bus and collect the given message types.
        
        We must not have connected to anything yet.
        """
        
        # We need to call the two inherited connection methods
        super()._set_bus_and_message_types(bus, wanted_types)
        self._set_bus(bus)


    
