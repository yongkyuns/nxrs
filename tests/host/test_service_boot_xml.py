"""Source/image replay, missing guest execution and personal-host refusals."""
import copy,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'gateway/tests'));sys.path.insert(0,str(ROOT/'tools/native-packages'))
import service_boot as host
import service_boot_guest as guest
import service_boot_xml as xml
import candidate

class ServiceBootXmlTests(unittest.TestCase):
    def plan(self):
        return dict(head='a'*40,system_xml_runtime=dict(qualification_sha256='b'*64,library_sha256='c'*64),
            files={str(xml.QUALIFICATION):'b'*64,str(xml.LIBRARY):'c'*64,str(xml.PROBE):'d'*64})

    def receipt(self):
        return dict(schema=1,scope=xml.SCOPE,proof=self.plan()['system_xml_runtime'],probe_sha256='d'*64,
            current_python_exact_image=True,c_abi_qualified=True,wrong_image_refused=True,
            unresolved_published_advisories=['CVE-2025-66382'],full_native_advisory_closure=False,deployment_permitted=False)

    def test_mixed_parent_library_and_probe_or_missing_execution_cannot_qualify(self):
        value=self.receipt();xml.validate(value,self.plan())
        mutations=[dict(proof=dict(qualification_sha256='e'*64,library_sha256='c'*64)),
            dict(proof=dict(qualification_sha256='b'*64,library_sha256='e'*64)),dict(probe_sha256='e'*64),
            dict(current_python_exact_image=False),dict(c_abi_qualified=False),dict(wrong_image_refused=False),
            dict(unresolved_published_advisories=[]),dict(full_native_advisory_closure=True),dict(deployment_permitted=True),
            dict(schema=True),dict(schema=2),dict(extra='private-value')]
        for mutation in mutations:
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):xml.validate({**value,**mutation},self.plan())
        plan=self.plan();plan['files'][str(xml.PROBE)]=True
        with self.assertRaises(ValueError):xml.validate({**value,'probe_sha256':True},plan)
        for name in self.plan()['files']:
            plan=self.plan();plan['files'].pop(name)
            with self.subTest(missing=name),self.assertRaises(ValueError):xml.validate(value,plan)

    def test_parent_xml_refuses_before_guest_allocation_and_native_build(self):
        with patch.object(host.p,'hosted'),patch.object(host,'inputs',return_value={}),\
             patch.object(host.p.u,'command',side_effect=['a'*40,'']),\
             patch.object(candidate,'xml_runtime',side_effect=ValueError('synthetic XML refusal')),\
             patch.object(host.package_boot,'prepare_guest') as prepare,\
             patch.object(host.system_xml_evidence,'private_read') as read:
            with self.assertRaisesRegex(ValueError,'synthetic XML refusal'):
                host.run(SimpleNamespace(work=Path('/synthetic/work'),package_work=Path('/synthetic/package')))
            prepare.assert_not_called();read.assert_not_called()

    def test_guest_context_refuses_before_xml_reads_and_native_operations(self):
        with patch.object(guest,'admitted',False),patch.object(candidate,'xml_runtime') as parent,\
             patch.object(guest,'command') as command:
            with self.assertRaisesRegex(ValueError,'guest-context-before-xml-qualification'):guest.qualify_xml(self.plan())
            parent.assert_not_called();command.assert_not_called()

    def test_actual_c_abi_and_wrong_image_control_are_required_between_current_python_checks(self):
        proof=self.plan()['system_xml_runtime']
        with patch.object(guest,'current_xml',return_value=proof) as current,\
             patch.object(guest,'command',side_effect=[b'',b'EXPAT_2_9_0_ABI_AND_LOADER_OK\n']) as command,\
             patch.object(guest,'digest',return_value='d'*64):
            value=guest.qualify_xml(self.plan())
        self.assertEqual(value,self.receipt());self.assertEqual(current.call_count,2)
        self.assertEqual(command.call_args_list[0].args[0],[xml.PROBE,xml.PROBE])
        self.assertEqual(command.call_args_list[0].kwargs,dict(accepted=(1,)))
        self.assertEqual(command.call_args_list[1].args[0],[xml.PROBE,xml.LIBRARY])
        for outputs in ([b'',b''],[b'',b'wrong private diagnostic']):
            with patch.object(guest,'current_xml',return_value=proof),\
                 patch.object(guest,'command',side_effect=outputs),self.assertRaises(ValueError):guest.qualify_xml(self.plan())
        with patch.object(guest,'current_xml',side_effect=[proof,dict(proof,library_sha256='e'*64)]),\
             patch.object(guest,'command',side_effect=[b'',b'EXPAT_2_9_0_ABI_AND_LOADER_OK\n']),self.assertRaises(ValueError):
            guest.qualify_xml(self.plan())

    def test_current_guest_interpreter_cannot_replay_a_different_parent_image(self):
        proof=self.plan()['system_xml_runtime']
        with patch.object(guest,'admitted',True),patch.object(candidate,'xml_runtime',return_value=dict(proof=proof)):
            self.assertEqual(guest.current_xml(self.plan()),proof)
        for name in proof:
            with patch.object(guest,'admitted',True),\
                 patch.object(candidate,'xml_runtime',return_value=dict(proof={**proof,name:'e'*64})),\
                 self.assertRaises(ValueError):guest.current_xml(self.plan())

if __name__=='__main__':unittest.main()
