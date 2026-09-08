"""Wire-compatible subset of vendor RPCs in an isolated descriptor pool."""
import json
import grpc
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

fd = descriptor_pb2.FileDescriptorProto(name='collection_wire.proto', package='collection_wire', syntax='proto3')
for name, fields in {
    'MetaRequest': [('json_config', 1, 9)], 'MetaData': [('json_data', 1, 9)],
    'StreamRequest': [('status_format', 1, 5)], 'DeviceStatus': [('format', 1, 5), ('json_data', 2, 9)],
}.items():
    msg = fd.message_type.add(name=name)
    for label, number, kind in fields:
        msg.field.add(name=label, number=number, type=kind, label=1)
pool = descriptor_pool.DescriptorPool()
pool.Add(fd)

def cls(name):
    descriptor = pool.FindMessageTypeByName('collection_wire.' + name)
    if hasattr(message_factory, 'GetMessageClass'):
        return message_factory.GetMessageClass(descriptor)
    return message_factory.MessageFactory(pool).GetPrototype(descriptor)

MetaRequest, MetaData, StreamRequest, DeviceStatus = map(cls, ['MetaRequest', 'MetaData', 'StreamRequest', 'DeviceStatus'])

class Vendor:
    def __init__(self, target='127.0.0.1:50051'):
        self.channel = grpc.insecure_channel(target, options=[('grpc.max_receive_message_length', 8 * 1024 * 1024)])

    def devices(self, timeout=60):
        rpc = self.channel.unary_stream('/robot.RobotService/DeviceStatusStream',
            request_serializer=StreamRequest.SerializeToString, response_deserializer=DeviceStatus.FromString)
        return rpc(StreamRequest(status_format=3), timeout=timeout)

    def meta(self, config, timeout=None):
        rpc = self.channel.unary_stream('/robot.RobotService/MetaTransfer',
            request_serializer=MetaRequest.SerializeToString, response_deserializer=MetaData.FromString)
        return rpc(MetaRequest(json_config=json.dumps(config, ensure_ascii=False)), timeout=timeout)

    def close(self):
        self.channel.close()
