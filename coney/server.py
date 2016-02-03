import logging
import pika
import traceback

from .compressors.null_compressor import NullCompressor
from .exceptions import DispatchHandlerException, HandlerNotCallableException, RemoteExecErrorException
from .request import Request
from .response import Response
from .response_codes import ResponseCodes
from .serializers.msgpack_serializer import MsgpackSerializer
import utils

logger = logging.getLogger(__name__)


class Server(object):
    def __init__(self, uri, prefetch_count=1, serializer=MsgpackSerializer, compressor=NullCompressor):
        self._serializer = serializer
        self._compressor = compressor

        params = pika.URLParameters(uri)
        self._connection = pika.BlockingConnection(parameters=params)
        self._channel = self._connection.channel()
        self._set_prefetch_count(prefetch_count)

        self._queues = {}

    def _set_prefetch_count(self, count):
        if self._channel:
            logger.debug('Channel prefetch count set to {}'.format(count))
            self._channel.basic_qos(prefetch_count=count)

    def _register_handler(self, method_name):
        def _dispatcher(ch, meth, props, raw_body):
            message = None
            try:
                body = self._compressor.decompress(raw_body)

                request = Request.loads(body, self._serializer)
                if not request:
                    raise DispatchHandlerException(ResponseCodes.MALFORMED_REQUEST)

                try:
                    method_versions = self._queues[method_name]
                except KeyError:
                    raise DispatchHandlerException(ResponseCodes.METHOD_NOT_FOUND)

                # Lookup the version specific handler
                try:
                    fn = method_versions[request.version]
                except KeyError:
                    raise DispatchHandlerException(ResponseCodes.VERSION_NOT_FOUND)

                return_value = fn(**request.arguments)

                resp = Response(return_value)
            except DispatchHandlerException as ex:
                logger.warn("Dispatch exception [{}] '{}': {}".format(
                    ex.code, ResponseCodes.describe(ex.code), raw_body
                ))
                resp = Response(None, ex.code)
            except RemoteExecErrorException as ex:
                logger.debug("Exec error [{}]: {}".format(ex.value, ex.details))
                resp = Response(None, ex.value, ex.details)
            except Exception as ex:
                logger.warn("Unhandled exception during method invocation:\n{}\nGenerated by: {}".format(
                    traceback.format_exc(), message))
                resp = Response(None, ResponseCodes.UNEXPECTED_DISPATCH_EXCEPTION, str(ex))

            # Publish response
            logger.debug("Replying with: {}".format(resp))
            ch.basic_publish(
                exchange='',
                routing_key=props.reply_to,
                properties=pika.BasicProperties(
                    correlation_id=props.correlation_id
                ),
                body=self._compressor.compress(resp.dumps(self._serializer))
            )

            # Ack the message broker
            ch.basic_ack(delivery_tag=meth.delivery_tag)

        self._channel.queue_declare(queue=method_name)
        self._channel.basic_consume(_dispatcher, queue=method_name)

    def register_handler(self, method_name, version, fn):
        if not utils.is_callable(fn):
            raise HandlerNotCallableException()

        try:
            if version in self._queues[method_name]:
                logger.warn('Duplicate handler registered for [{}/{}]').format(method_name, version)
        except KeyError:
            # Register version handler and method dispatcher
            self._queues[method_name] = {version: fn}
            self._register_handler(method_name)
        else:
            # Register version handler
            self._queues[method_name][version] = fn

    def run(self):
        self._channel.start_consuming()

