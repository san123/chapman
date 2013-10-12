import logging
from datetime import datetime
from cPickle import loads
from random import random

from mongotools.util import LazyProperty
from mongotools.pubsub import Channel
from ming import Field
from ming.declarative import Document
from ming import schema as S

from .m_base import doc_session, dumps
from .m_task import TaskState

log = logging.getLogger(__name__)


class ChannelProxy(object):

    def __init__(self, name):
        self._name = name

    @LazyProperty
    def _channel(self):
        return self.new_channel()

    def __getattr__(self, name):
        return getattr(self._channel, name)

    def __get__(self, obj, cls=None):
        if obj is None:
            return self
        return self._channel

    def new_channel(self):
        return Channel(doc_session.db, self._name)


class Message(Document):
    missing_worker = '-' * 10
    channel = ChannelProxy('chapman.event')

    class __mongometa__:
        name = 'chapman.message'
        session = doc_session
        indexes = [
            [('s.status', 1), ('s.pri', -1), ('s.ts', 1), ('s.q', 1)],
            [('s.q', 1), ('s.status', 1), ('s.pri', -1), ('s.ts', 1)],
            [('task_id', 1)],
        ]
    _id = Field(int, if_missing=lambda: hash(random()))
    task_id = Field(int, if_missing=None)
    task_repr = Field(str, if_missing=None)
    slot = Field(str)
    _args = Field('args', S.Binary)
    _kwargs = Field('kwargs', S.Binary)
    _send_args = Field('send_args', S.Binary)
    _send_kwargs = Field('send_kwargs', S.Binary)
    schedule = Field('s', dict(
        status=S.String(if_missing='pending'),
        ts=S.DateTime(if_missing=datetime.utcnow),
        after=S.DateTime(if_missing=datetime.utcnow),
        q=S.String(if_missing='chapman'),
        pri=S.Int(if_missing=10),
        w=S.String(if_missing=missing_worker)))

    def __repr__(self):
        return '<msg (%s) %s to %s %s on %s>' % (
            self.schedule.status, self._id, self.slot, self.task_repr,
            self.schedule.w)

    @classmethod
    def n(cls, task, slot, *args, **kwargs):
        '''Convenience method for Message.new'''
        return cls.new(task, slot, args, kwargs)

    @classmethod
    def new(cls, task, slot, args, kwargs, after=None):
        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}
        self = cls.make(dict(
            task_id=task.id,
            task_repr=repr(task),
            slot=slot,
            s=task.schedule_options()))
        if after is not None:
            self.s.after = after
        self.args = args
        self.kwargs = kwargs
        self.m.insert()
        return self

    @classmethod
    def _reserve_next(cls, worker, queues):
        '''Reserves a message in 'next' status.

        'next' messages can be immediately worked on, since they are guaranteed
        to be the next message to obtain the task lock.
        '''
        self = cls.m.find_and_modify(
            {'s.status': 'next',
             's.q': {'$in': queues}},
            sort=[('s.pri', -1), ('s.ts', 1)],
            update={'$set': {'s.w': worker, 's.status': 'busy'}},
            new=True)
        if self is None:
            return None, None
        state = TaskState.m.get(_id=self.task_id)
        return self, state

    @classmethod
    def _reserve_ready(cls, worker, queues):
        '''Reserves a message in 'ready' status.

        Ready messages must move through q1 status before they become
        'busy', since there may already be a message locking the task.
        If there is already a message locking the task, the state is set to q2.
        '''
        # Reserve message
        now = datetime.utcnow()
        self = cls.m.find_and_modify(
            {'s.status': 'ready',
             's.q': {'$in': queues},
             's.after': {'$lte': now}},
            sort=[('s.pri', -1), ('s.ts', 1)],
            update={'$set': {'s.w': worker, 's.status': 'q1'}},
            new=True)
        if self is None:
            return None, None
        # Enqueue on TaskState
        state = TaskState.m.find_and_modify(
            {'_id': self.task_id},
            update={'$push': {'mq': self._id}},
            new=True)
        if state is None:
            return self, None
        if state.mq[0] == self._id:
            # We are the first in the queue, so we get to go
            self.m.set({'s.status': 'busy'})
            return self, state
        else:
            # Not the first, so set to q2
            cls.m.update_partial(
                {'_id': self._id, 's.status': 'q1'},
                {'$set': {'s.status': 'q2',
                          's.w': cls.missing_worker}})
            return self, None

    def unlock(self):
        '''Make a message ready for processing'''
        # Dequeue the message from any taskstate it's on
        state = TaskState.m.find_and_modify(
            {'_id': self.task_id},
            update={'$pull': {'mq': self._id}},
            new=True)
        # If this task now has a q2 task at the front of the queue, it must be
        # activated.
        if state and state.mq:
            next_id = state.mq[0]
            r = Message.m.collection.update(
                {'_id': next_id, 's.status': 'q2'},
                {'$set': {'s.status': 'next'}})
            if r['updatedExisting']:
                self.channel.pub('send', next_id)
        # Re-dispatch this message
        Message.m.update_partial(
            {'_id': self._id},
            {'$set': {
                's.status': 'ready',
                's.w': self.missing_worker}})
        self.channel.pub('send', self._id)

    @classmethod
    def reserve(cls, worker, queues):
        '''Reserve a message & try to lock the task state.

        - If no message could be reserved, return (None, None)
        - If a message was reserved, but the task could not be locked, return
          (msg, None)
        - If a message was reserved, and the task was locked, return
          (msg, task)
        '''
        msg, state = cls._reserve_next(worker, queues)
        if state is not None:
            return msg, state
        return cls._reserve_ready(worker, queues)

    def retire(self):
        '''Retire the message.'''
        state = TaskState.m.find_and_modify(
            {'_id': self.task_id},
            update={'$pull': {'mq': self._id}},
            new=True)
        if state is not None and state.mq:
            next_msg = Message.m.find_and_modify(
                {'_id': state.mq[0],
                 's.status': {'$in': ['q1', 'q2']}},
                update={'$set': {'s.status': 'next'}},
                new=True)
            if next_msg:
                self.channel.pub('send', next_msg._id)
        self.m.delete()

    def retire_and_chain(self):
        '''Retire the message. If there is a message enqueued,
        reserve and return it. Otherwise return None.
        '''
        state = TaskState.m.find_and_modify(
            {'_id': self.task_id},
            update={'$pull': {'mq': self._id}},
            new=True)
        next_msg = None
        if state is not None and state.mq:
            next_msg = Message.m.find_and_modify(
                {'_id': state.mq[0], 's.status': {'$in': ['q1', 'q2']}},
                update={'$set': {'s.w': self.s.w, 's.status': 'busy'}},
                new=True)
        self.m.delete()
        return next_msg

    def send(self, *args, **kwargs):
        self.m.set(
            {'s.status': 'ready',
             's.ts': datetime.utcnow(),
             'send_args': dumps(args),
             'send_kwargs': dumps(kwargs)})
        self.channel.pub('send', self._id)

    @property
    def args(self):
        result = []
        if self._send_args is not None:
            result += loads(self._send_args)
        if self._args is not None:
            result += loads(self._args)
        return tuple(result)

    @args.setter
    def args(self, value):
        self._args = dumps(value)

    @property
    def kwargs(self):
        result = {}
        if self._kwargs is not None:
            result.update(loads(self._kwargs))
        if self._send_kwargs is not None:
            result.update(loads(self._send_kwargs))
        return result

    @kwargs.setter
    def kwargs(self, value):
        self._kwargs = dumps(value)
