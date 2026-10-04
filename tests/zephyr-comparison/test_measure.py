import unittest
from measure import validate


def output(ack=28):
    return (f"CQ_TRANSPORT_PASS language=zephyr mode=large cycles=1000000 "
            f"event_bytes=248 ack_bytes={ack}\n"
            "CQ_ZEPHYR_SCALE_PASS mode=large queues=60 logical_streams=60 "
            "threads=20 messages=5760 digest=441445568\n"
            f"ZEPHYR_RESOURCES queue_buffers={45*248 + 15*4*ack} queue_objects=3420 "
            "thread_objects=4480 stack_storage=83968 entry_storage=240 heap_allocated=0\n"
            "ZEPHYR_MEMORY kernel_heap_reserved=4096 kernel_heap_used=100 "
            "kernel_heap_peak=200 kernel_heap_free=3700\n"
            "ZEPHYR_COMMAND_EXIT status=0\nzephyr> ").encode()


class MeasurementTests(unittest.TestCase):
    def test_packet_and_wire_contracts(self):
        self.assertEqual(validate(output(), "large", "packet")["scale"]["messages"], 5760)
        self.assertEqual(validate(output(16), "large", "wire")["transport"]["ack_bytes"], 16)

    def test_mismatches_rejected(self):
        for old, new in ((b"queues=60", b"queues=59"), (b"digest=441445568", b"digest=0"),
                         (b"status=0", b"status=1"), (b"cycles=1000000", b"cycles=0"),
                         (b"stack_storage=83968", b"stack_storage=100000"),
                         (b"queue_buffers=12840", b"queue_buffers=12841")):
            with self.assertRaises(ValueError):
                validate(output().replace(old, new), "large", "packet")
        with self.assertRaises(ValueError):
            validate(output(), "large", "wire")
        with self.assertRaises(ValueError):
            validate(output() + output(), "large", "packet")


if __name__ == "__main__":
    unittest.main()
