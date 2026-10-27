"""Require actual signed kernel startup/handoff; invalid images cannot enter it."""
import copy
import struct
import unittest

import qualify_nuttx_boot as startup
import boot_execution


class NativeGeometry(unittest.TestCase):
    def fixture(self):
        layout = dict(zip(startup.FIELDS, [0xace637d3,96,4,0,4,8,12,16,20,24,28,
                                          0x40370000,0x403e0000,0x3fc88000,0x3fd00000]))
        body = bytearray(256)
        struct.pack_into('<8I',body,0,layout['magic'],0x40378004,0x40378000,128,32,0x3fc90000,160,32)
        return layout,bytes(body)

    def test_actual_header_field_offsets_define_slot_relative_segments(self):
        layout,body = self.fixture()
        geometry = startup.payload_geometry(body,layout,32)
        self.assertEqual(geometry['entry'],0x40378004)
        self.assertEqual(geometry['iram'],dict(address=0x40378000,image_offset=128,bytes=32))

    def test_wrong_magic_entry_geometry_or_truncated_payload_is_refused(self):
        layout,body = self.fixture()
        for index,value in [(0,0),(1,0x42000000),(2,0x1000),(3,32),(4,0xffffffff),(5,0),(6,0xffffffff)]:
            altered=bytearray(body);struct.pack_into('<I',altered,index*4,value)
            with self.subTest(index=index),self.assertRaises(ValueError):
                startup.payload_geometry(bytes(altered),layout,32)
        with self.assertRaises(ValueError):
            startup.payload_geometry(body[:100],layout,32)


class StartupEvidence(unittest.TestCase):
    def fixture(self):
        kinds=['watchdog-init','watchdog-hal-init','boot-go','hook','watchdog-progress','hook-return',
               'stock-check','stock-signature','image-load','nuttx-entry','nuttx-chip-start',
               'nuttx-wdt-entry','nuttx-wdt-return','nuttx-after-wdt']
        events=[dict(kind=kind,pc=0x40000000+4*i,sp=0x3fc91000) for i,kind in enumerate(kinds)]
        for event in events:
            if event['kind']=='hook-return':event['value']=7
            if event['kind'].startswith('nuttx-') and event['kind']!='nuttx-wdt-return':event['code_matches_elf']=True
        return events,dict(low=0x3fc90000,bytes=8192)

    def test_full_native_handoff_order_and_own_idle_stack_are_required(self):
        events,stack=self.fixture()
        result=startup.check_events(startup.POSITIVE,events,7,stack)
        self.assertTrue(result['startup_handoff_observed'])

    def test_missing_duplicate_and_reordered_events_fail(self):
        events,stack=self.fixture()
        for index in range(len(events)):
            if events[index]['kind']=='watchdog-progress':continue
            with self.subTest(index=index),self.assertRaises(ValueError):
                startup.check_events(startup.POSITIVE,events[:index]+events[index+1:],7,stack)
        with self.assertRaises(ValueError):
            startup.check_events(startup.POSITIVE,events[:1]+events,7,stack)
        swapped=copy.deepcopy(events);swapped[-2],swapped[-3]=swapped[-3],swapped[-2]
        with self.assertRaises(ValueError):startup.check_events(startup.POSITIVE,swapped,7,stack)

    def test_wrong_code_or_handoff_on_bootloader_stack_is_refused(self):
        events,stack=self.fixture()
        for kind in ('nuttx-entry','nuttx-chip-start','nuttx-wdt-entry','nuttx-after-wdt'):
            altered=copy.deepcopy(events);next(e for e in altered if e['kind']==kind)['code_matches_elf']=False
            with self.assertRaises(ValueError):startup.check_events(startup.POSITIVE,altered,7,stack)
        next(e for e in events if e['kind']=='nuttx-wdt-entry')['sp']=0x3fceb000
        with self.assertRaises(ValueError):startup.check_events(startup.POSITIVE,events,7,stack)

    def test_each_invalid_full_kernel_requires_actual_no_image_halt(self):
        events,stack=self.fixture()
        for label in ('signed-nuttx-wrong-target','signed-nuttx-signature-corruption','unsigned-nuttx'):
            if label.endswith('wrong-target'):
                refused=events[:6]+[dict(kind='no-image')];refused[5]=dict(kind='hook-return',value=8)
            elif label.endswith('signature-corruption'):refused=events[:8]+[dict(kind='no-image')]
            else:refused=events[:3]+[dict(kind='no-image')]
            self.assertFalse(startup.check_events(label,refused,7,stack)['startup_handoff_observed'])
            with self.assertRaises(ValueError):startup.check_events(label,refused[:-1]+[dict(kind='nuttx-entry')],7,stack)


class DebugFailureProtocol(unittest.TestCase):
    def test_bounded_interrupt_response_requires_the_same_checksum(self):
        class Connection:
            def __init__(self):self.raw=b'$T02thread:p01.01;#'+f'{sum(b"T02thread:p01.01;")%256:02x}'.encode();self.sent=[]
            def sendall(self,raw):self.sent.append(raw)
            def recv(self,size):part,self.raw=self.raw[:size],self.raw[size:];return part
            def settimeout(self,value):pass
        connection=Connection();remote=boot_execution.Remote(connection,0)
        self.assertEqual(remote.interrupt(),b'T02thread:p01.01;')
        self.assertEqual(connection.sent,[b'\x03',b'+'])


if __name__=='__main__':unittest.main()
