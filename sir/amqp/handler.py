#!/usr/bin/env python
# coding: utf-8
# Copyright (c) 2014, 2015 Wieland Hoffmann
# License: MIT, see LICENSE for details
from sir.amqp import message
from sir import get_sentry, config
from sir.schema import SCHEMA, generate_update_map
from sir.indexing import live_index
from sir.trigger_generation.paths import second_last_model_in_path, generate_query, generate_filtered_query
from sir.util import (create_amqp_connection,
                      db_session,
                      db_session_ctx,
                      solr_connection,
                      solr_version_check,
                      ReusableTimer)
from amqp.exceptions import AMQPError
from functools import partial, wraps
from logging import getLogger
from retrying import retry
from socket import error as socket_error
from sqlalchemy.orm import class_mapper
from sys import exit
from urllib2 import URLError
from ConfigParser import NoOptionError
from collections import defaultdict
from multiprocessing import Lock, active_children
import signal

__all__ = ["callback_wrapper", "watch", "Handler"]

logger = getLogger("sir")

update_map, column_map, model_map = generate_update_map()

#: The number of times we'll try to process a message.
_DEFAULT_MB_RETRIES = 4

#: The number of seconds between each connection attempt to the AMQP server.
_RETRY_WAIT_SECS = 30

# Tables which are core entities, but do not have a guid.
# These will be deleted via their `id`.
_ID_DELETE_TABLE_NAMES = ['annotation', 'tag', 'release_raw', 'editor']


def requeue_message(msg, exc):
    if not hasattr(msg, "application_headers"):
        # TODO(roman): Document when this might happen
        logger.warning("Message doesn't have \"application_headers\" attribute",
                       extra={"msg": msg, "attributes": msg.__dict__})
        msg.application_headers = {}
    retries_remaining = msg.application_headers.get("mb-retries", _DEFAULT_MB_RETRIES)
    routing_key = msg.delivery_info["routing_key"]
    if retries_remaining:
        msg.application_headers["mb-retries"] = retries_remaining - 1
        msg.application_headers["mb-exception"] = str(exc)
        msg.channel.basic_publish(msg, exchange="search.retry", routing_key=routing_key)
    else:
        msg.channel.basic_publish(msg, exchange="search.failed", routing_key=routing_key)


def callback_wrapper(f):
    """
    Common wrapper for a message callback function that provides basic sanity
    checking for messages and provides exception handling for a function it wraps.

    The following wrapper function is returned:

    .. py:function:: wrapper(self, msg, queue)

        :param sir.amqp.handler.Handler self: Handler object that is processing a message.
        :param amqp.basic_message.Message msg: Message itself.
        :param str queue: Name of the queue that the message has originated from.

        Calls ``f`` with ``self`` and an instance of :class:`~sir.amqp.message.Message`.
        If an exception gets raised by ``f``, it will be caught and the message will be
        :meth:`rejected <amqp:amqp.channel.Channel.basic_reject>` and sent to the
        ``search.failed`` queue (cf. :ref:`queue_setup`). Then the exception will not be
        reraised.

        If no exception is raised, the message will be :meth:`acknowledged
        <amqp:amqp.channel.Channel.basic_ack>`.

    """
    @wraps(f)
    def wrapper(self, msg, queue):
        try:
            logger.debug("Received message from queue %s: %s" % (queue, msg.body))
            parsed_message = message.Message.from_amqp_message(queue, msg)
            if parsed_message.table_name not in update_map:
                raise ValueError("Unknown table: %s" % parsed_message.table_name)
            f(self=self, parsed_message=parsed_message)
            with self.queue_lock:
                self.pending_messages.append(msg)
            if len(self.pending_messages) >= self.batch_size:
                self.process_messages()
        except Exception as exc:
            logger.error(exc, extra={"data": {"message": vars(msg)}})
            msg.channel.basic_reject(msg.delivery_tag, requeue=False)
            requeue_message(msg, exc)

    return wrapper


class Handler(object):
    """
    This class is used to provide callbacks for AMQP messages and access to
    Solr cores.
    """

    def __init__(self):
        self.cores = {}
        for core_name in SCHEMA.keys():
            self.cores[core_name] = solr_connection(core_name)
            solr_version_check(core_name)

        # Used to define the batch size of the pending messages list
        try:
            self.batch_size = config.CFG.getint("sir", "live_index_batch_size")
        except (NoOptionError, AttributeError):
            self.batch_size = 1
        # Defines how long the handler should wait before processing messages.
        # Used to trigger the process_message callback to prevent starvation
        # in pending_messages in case it doesn't fill up to batch_size
        try:
            self.process_delay = config.CFG.getint("sir", "process_delay")
        except (NoOptionError, AttributeError):
            self.process_delay = 120

        logger.info("Batch size is set to %s", self.batch_size)
        logger.info("Process delay is set to %s seconds", self.process_delay)

        self.db_session = db_session()
        self.pending_messages = []
        self.pending_entities = defaultdict(set)
        self.process_timer = ReusableTimer(self.process_delay, self.process_messages)
        self.queue_lock = Lock()

    @callback_wrapper
    def index_callback(self, parsed_message):
        """
        Callback for processing `index` messages.

        Messages for indexing have the following format:

            <table name>, PKs{<PK row name>, <PK value>}

        First value is a table name, followed by primary key values for that
        table. These are then used to lookup values that need to be updated.
        For example:

            {"_table": "artist_credit_name", "position": 0, "artist_credit": 1}

        In this handler we are doing a selection with joins which follow a "path"
        from a table that the trigger was received from to an entity (later
        "core", https://wiki.apache.org/solr/SolrTerminology). To know which
        data to retrieve we are using PK(s) of a table that was updated.
        `update_map` provides us with a view of dependencies between entities
        (cores) and all the tables. So if data in some table has been updated,
        we know which entities store this data in the index and need to be
        refreshed.

        :param sir.amqp.message.Message parsed_message: Message parsed by the `callback_wrapper`.
        """
        logger.info("Processing `index` message from table: %s" % parsed_message.table_name)
        logger.info("Message columns %s" % parsed_message.columns)
        if parsed_message.operation == 'delete':
            self._index_by_fk(parsed_message)
        else:
            self._index_by_pk(parsed_message)

    @callback_wrapper
    def delete_callback(self, parsed_message):
        """
        Callback for processing `delete` messages.

        Messages for deletion have the following format:

            <table name>, <gid>

        First value is a table name for an entity that has been deleted.
        Second is GID of the row in that table. For example:

            {"_table": "release", "gid": "90d7709d-feba-47e6-a2d1-8770da3c3d9c"}

        This callback function is expected to receive messages only from
        entity tables all of which have a `gid` column on them except the ones
        in `_ID_DELETE_TABLE_NAMES` which are deleted via their `id`.

        :param sir.amqp.message.Message parsed_message: Message parsed by the `callback_wrapper`.
        """

        column_name = "gid"

        if "gid" not in parsed_message.columns:
            if "id" in parsed_message.columns and parsed_message.table_name in _ID_DELETE_TABLE_NAMES:
                column_name = "id"
            else:
                raise ValueError("`gid` column missing from delete message")
        logger.info("Deleting {entity_type}: {id}".format(
            entity_type=parsed_message.table_name,
            id=parsed_message.columns[column_name]))
        self.cores[parsed_message.table_name.replace("_", "-")].delete(parsed_message.columns[column_name])
        self._index_by_fk(parsed_message)

    def process_messages(self):
        with self.queue_lock:
            try:
                live_index(self.pending_entities)
            except Exception as exc:
                for msg in self.pending_messages:
                    logger.error(exc, extra={"data": {"message": vars(msg)}})
                    requeue_message(msg, exc)
            else:
                for msg in self.pending_messages:
                    msg.channel.basic_ack(msg.delivery_tag)
                self.pending_messages = []
                self.pending_entities.clear()

    def _index_data(self, core_name, id_list):
        logger.info("Queueing %s new rows for entity %s", len(id_list), core_name)
        with self.queue_lock:
            self.pending_entities[core_name].update(set(id_list))
        # Reset the timer for the callback to process_messages
        self.process_timer.restart()

    def _index_by_pk(self, parsed_message):
        for core_name, path in update_map[parsed_message.table_name]:
            # Going through each core/entity that needs to be updated
            # depending on which table we receive a message from.
            entity = SCHEMA[core_name]
            with db_session_ctx(self.db_session) as session:

                if path is None:
                    # If `path` is `None` then we received a message for an entity itself
                    ids = [parsed_message.columns["id"]]
                else:
                    # otherwise it's a different table...
                    logger.info("Generating SELECT statement for %s with path '%s'" % (entity.model, path))
                    select_query = generate_filtered_query(entity.model, path, parsed_message.columns)
                    if select_query is None:
                        logger.warning("SELECT is `None`")
                        continue
                    else:
                        logger.debug("SQL: %s" % select_query)
                        ids = [row[0] for row in session.execute(select_query).fetchall()]

                # Retrieving actual data
                self._index_data(core_name, ids)

    def _index_by_fk(self, parsed_message):
        index_model = model_map[parsed_message.table_name]
        # We need to construct this since we only need to update tables which
        # have 'many to one' relationship with the table represented by `index_model`,
        # since index_by_fk is only called when an entity is deleted and we need
        # to update the related entities. For 'one to many' relationships, the related
        # entity would have had an update trigger firing off to unlink the `index_entity`
        # before `index_entity` itself is deleted, so we can ignore those.
        relevant_rels = dict((r.table.name, (r.key, list(r.remote_side)[0]))
                             for r in class_mapper(index_model).mapper.relationships
                             if r.direction.name == 'MANYTOONE')
        for core_name, path in update_map[parsed_message.table_name]:
            # Going through each core/entity that needs to be updated
            # depending on original index model
            entity = SCHEMA[core_name]
            # We are finding the second last model in path, since the rows related to
            # `index_model` are deleted and the sql query generated from that path
            # returns no ids, because of the way select query is generated.
            # We generate sql queries with the second last model in path, since that
            # will be related to the `index_model` by a FK and we can thus determine
            # the tables to be updated from the FK values emitted by the delete triggers
            related_model, new_path = second_last_model_in_path(entity.model, path)
            related_table_name = ""
            if related_model:
                related_table_name = class_mapper(related_model).mapped_table.name
            if related_table_name in relevant_rels:
                with db_session_ctx(self.db_session) as session:
                    if new_path is None:
                        # If `path` is `None` then we received a message for an entity itself
                        ids = [parsed_message.columns['id']]
                    else:
                        logger.info("Generating SELECT statement for %s with path '%s'" % (entity.model, new_path))
                        fk_name, remote_key = relevant_rels[related_table_name]
                        filter_expression = remote_key.__eq__(parsed_message.columns[fk_name])
                        # If `new_path` is blank, then the given table, was directly related to the
                        # `index_model` by a FK.
                        select_query = generate_query(entity.model, new_path, filter_expression)
                        if select_query is None:
                            logger.warning("SELECT is `None`")
                            continue
                        ids = [row[0] for row in session.execute(select_query).fetchall()]
                        logger.debug("SQL: %s" % (select_query))

                    # Retrieving actual data
                    self._index_data(core_name, ids)


def _should_retry(exc):
    logger.debug("Retrying...")
    logger.exception(exc)
    if isinstance(exc, (SystemExit, KeyboardInterrupt)):
        return False
    if isinstance(exc, AMQPError) or isinstance(exc, socket_error):
        logger.info("Retrying in %i seconds", _RETRY_WAIT_SECS)
        return True

    return False


@retry(wait_fixed=_RETRY_WAIT_SECS * 1000, retry_on_exception=_should_retry)
def _watch_impl():

    def add_handler(queue, f):
        logger.info("Adding a callback to %s", queue)
        handler = partial(f, queue=queue)
        ch.basic_consume(queue, callback=handler)

    try:
        conn = create_amqp_connection()
        logger.info("Connection to RabbitMQ established")
        logger.debug("Heartbeat value: %s" % conn.heartbeat)
        ch = conn.channel()
        # Keep in mind that `prefetch_size` is not supported by the version of RabbitMQ that
        # we are currently using (https://www.rabbitmq.com/specification.html).
        # Limits are requires because consumer connection might time out when receive buffer
        # is full (http://stackoverflow.com/q/35438843/272770).
        prefetch_count = config.CFG.getint("rabbitmq", "prefetch_count")
        ch.basic_qos(prefetch_size=0, prefetch_count=prefetch_count, a_global=True)

        handler = Handler()
        add_handler("search.index", handler.index_callback)
        add_handler("search.delete", handler.delete_callback)

        def signal_handler(signum, frame):
            try:
                handler.process_timer.cancel()
            except Exception:
                # In case the timer is not active it throws an exception
                # Simply ignore it.
                pass
            try:
                for msg in handler.pending_messages:
                    requeue_message(msg, "")
            except Exception as exc:
                logger.error(exc)
            children = active_children()
            for child in children:
                child.terminate()
                child.join()
            exit(0)

        signal.signal(signal.SIGTERM, signal_handler)

        while True:
            logger.debug("Waiting for a message")
            conn.drain_events()
    except Exception:
        get_sentry().captureException()
        raise


def watch(args):
    """
    Watch AMQP queues for messages.

    :param args: will be ignored
    """
    try:
        create_amqp_connection()
    except socket_error as e:
        logger.error("Couldn't connect to RabbitMQ, check your settings. %s", e)
        exit(1)

    try:
        _watch_impl()
    except URLError as e:
        logger.error("Connecting to Solr failed: %s", e)
        exit(1)
