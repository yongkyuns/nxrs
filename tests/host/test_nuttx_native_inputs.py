"""Real compiler execution and coherent native-input refusal controls; no device I/O."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which('cc'),'host C compiler required')
class NativeInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='nxrs-native-inputs-')
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name).resolve()
        tool=self.root/'tool';tool.mkdir()
        self.producer=tool/'nuttx_native_inputs.py';shutil.copyfile(ROOT/'tools/nuttx_native_inputs.py',self.producer)
        spec=importlib.util.spec_from_file_location('native_input_fixture',self.producer)
        self.capture=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.capture)
        self.source=self.root/'source with spaces';self.source.mkdir()
        self.header=self.source/'value header.h';self.header.write_text('#define FIXTURE_VALUE 23\n')
        self.unit=self.source/'unit.c';self.unit.write_text('#include "value header.h"\nint fixture(void) {return FIXTURE_VALUE;}\n')
        self.obj=self.root/'built.o';self.directory=self.root/'capture'
        self.cc=str(Path(shutil.which('cc')).absolute())
        self.origins={'source':self.source}
        if sys.platform=='darwin':
            sdk=subprocess.check_output(['xcrun','--show-sdk-path'],text=True).strip()
            self.origins['host-sdk']=Path(sdk).resolve()
        else:
            for name,path in [('host-include','/usr/include'),('host-local-include','/usr/local/include')]:
                if Path(path).is_dir():self.origins[name]=Path(path).resolve()

    def prepare(self,compiler=None):
        self.capture.prepare(self.directory,compiler or self.cc,None,self.origins)

    def compile(self,extra=()):
        return subprocess.run([str(self.directory/'bin/gcc'),*extra,'-c',str(self.unit),'-o',str(self.obj)],
                              cwd=self.source,capture_output=True,timeout=20)

    def report(self):return self.capture.collect(self.directory,self.obj)

    def execute_object(self, obj):
        caller=self.root/'caller.c';caller.write_text('extern int fixture(void); int main(void) {return fixture()==23 ? 0 : 1;}\n')
        binary=self.root/'caller'
        result=subprocess.run([self.cc,str(caller),str(obj),'-o',str(binary)],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,'captured object must link with the real compiler')
        self.assertEqual(subprocess.run([str(binary)],capture_output=True,timeout=5).returncode,0)

    def test_real_compilation_binds_headers_object_and_compiler_without_private_command_disclosure(self):
        self.prepare();self.assertEqual(self.compile().returncode,0)
        self.execute_object(self.obj)
        report=self.report()
        self.assertEqual(report['compiled_units'],1)
        self.assertEqual(report['inputs']['source:unit.c'],self.capture.digest(self.unit))
        self.assertEqual(report['inputs']['source:value header.h'],self.capture.digest(self.header))
        self.assertGreaterEqual(len(report['inputs']),2)
        self.assertEqual(report['units'][0]['object_sha256'],self.capture.digest(self.obj))
        self.assertEqual(report['compilers']['gcc'],self.capture.digest(self.cc))
        self.assertNotIn(str(self.root),json.dumps(report))
        self.assertNotIn('arguments',json.dumps(report))
        for name in ['retained_link_coverage_qualified','full_firmware_inventory_qualified',
                     'native_advisory_closure','rust_input_closure_qualified','physical_qualified','deployment_permitted']:
            self.assertFalse(report[name])

    def test_actual_cmake_launcher_compiler_argument_is_checked_and_captured(self):
        self.prepare()
        result=subprocess.run([str(self.directory/'bin/gcc-launcher'),self.cc,
            '-c',str(self.unit),'-o',str(self.obj)],cwd=self.source,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0)
        self.assertEqual(self.report()['compiled_units'],1)

    def test_actual_cmake_configuration_and_build_capture_only_real_target_units(self):
        cmake=Path(sys.executable).parent/'cmake'
        if not cmake.is_file():
            located=shutil.which('cmake')
            if not located:self.skipTest('CMake required for real launcher integration')
            cmake=Path(located)
        (self.source/'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.20)\nproject(capture C)\nadd_library(probe OBJECT unit.c)\n')
        build=self.root/'cmake-build'
        def run(*arguments):
            result=subprocess.run([str(cmake),*map(str,arguments)],cwd=self.root,capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        run('-S',self.source,'-B',build,'-DCMAKE_C_COMPILER='+self.cc)
        self.prepare()
        run('-S',self.source,'-B',build,'-DCMAKE_C_COMPILER_LAUNCHER='+str(self.directory/'bin/gcc-launcher'))
        run('--build',build)
        records=list((self.directory/'records').glob('*/unit.json'))
        self.assertEqual(len(records),1,'compiler probes must not count as target compilation')
        record=json.loads(records[0].read_bytes())
        self.execute_object(Path(record['object']))
        report=self.capture.collect(self.directory,Path(record['object']))
        self.assertEqual(report['compiled_units'],1)
        self.assertEqual(report['inputs']['source:unit.c'],self.capture.digest(self.unit))
        self.assertEqual(report['compilers']['gcc'],self.capture.digest(self.cc))

    def test_real_compilation_preserves_requested_dependency_file_and_quoted_targets(self):
        self.prepare();requested=self.root/'original dependencies.d'
        result=self.compile(['-MMD','-MF',str(requested),'-MT','requested-target.c','-MQ','quoted target'])
        self.assertEqual(result.returncode,0)
        raw=requested.read_text()
        self.assertIn('requested-target.c',raw)
        self.assertIn('quoted\\ target',raw)
        self.assertIn('value\\ header.h',raw)
        report=self.report()
        self.assertEqual(report['compiled_units'],1)
        self.assertEqual(report['inputs']['source:unit.c'],self.capture.digest(self.unit))

    def test_launcher_refuses_different_compiler_without_executing_it(self):
        self.prepare()
        result=subprocess.run([str(self.directory/'bin/gcc-launcher'),sys.executable,
            '-c',str(self.unit),'-o',str(self.obj)],cwd=self.source,capture_output=True,timeout=20)
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'category=compiler-identity',result.stderr)
        self.assertNotIn(str(self.root).encode(),result.stderr)
        self.assertFalse(self.obj.exists())
        self.assertFalse(list((self.directory/'records').glob('*/unit.json')))

    def test_response_file_arguments_are_actually_compiled(self):
        self.prepare();response=self.root/'arguments.rsp'
        # GCC response-file quoting must preserve the actual header directory.
        response.write_text('-DFIXTURE_EXTRA=4 -I"'+str(self.source)+'"')
        self.assertEqual(self.compile(['@'+str(response)]).returncode,0)
        self.assertEqual(self.report()['compiled_units'],1)

    @unittest.skipUnless(shutil.which('c++'),'host C++ compiler required')
    def test_real_cpp_and_preprocessed_assembly_are_captured(self):
        self.capture.prepare(self.directory,self.cc,shutil.which('c++'),self.origins)
        cpp=self.source/'unit.cpp'
        cpp.write_text('#include "value header.h"\ntemplate<int N> int value(){return N;}\nint cpp_fixture(){return value<FIXTURE_VALUE>();}\n')
        cpp_obj=self.root/'cpp.o'
        result=subprocess.run([str(self.directory/'bin/g++'),'-c',str(cpp),'-o',str(cpp_obj)],
                              cwd=self.source,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0)
        assembly=self.source/'unit.S'
        assembly.write_text('#include "value header.h"\n.text\n.globl nxrs_fixture\nnxrs_fixture:\n.byte FIXTURE_VALUE\n')
        asm_obj=self.root/'assembly.o'
        result=subprocess.run([str(self.directory/'bin/gcc'),'-c',str(assembly),'-o',str(asm_obj)],
                              cwd=self.source,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0)
        report=self.capture.collect(self.directory,cpp_obj)
        self.assertEqual({unit['source'] for unit in report['units']},{'source:unit.cpp','source:unit.S'})

    def test_dependency_query_never_counts_as_object_compilation(self):
        self.prepare()
        result=subprocess.run([str(self.directory/'bin/gcc'),'-c',str(self.unit),'-M','-MF',str(self.root/'query.d')],
                              cwd=self.source,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0)
        self.assertFalse(list((self.directory/'records').glob('*/unit.json')))
        with self.assertRaises(ValueError):self.report()

    def test_changed_header_object_or_producer_is_refused_then_restoration_passes(self):
        self.prepare();self.assertEqual(self.compile().returncode,0)
        for path in [self.header,self.obj,self.producer]:
            with self.subTest(path=path.name):
                original=path.read_bytes()
                try:
                    path.write_bytes(original+b'altered')
                    with self.assertRaises(ValueError):self.report()
                finally:path.write_bytes(original)
                self.assertTrue(self.report()['qualified'])

    def test_missing_records_unknown_origin_and_duplicate_object_records_are_refused(self):
        self.prepare()
        with self.assertRaises(ValueError):self.report()
        self.assertEqual(self.compile().returncode,0)
        config_path=self.directory/'configuration.json';original=config_path.read_bytes()
        config=json.loads(original);config['origins']={'elsewhere':str(self.root/'unrelated')}
        config_path.write_text(json.dumps(config))
        with self.assertRaises(ValueError):self.report()
        config_path.write_bytes(original)
        self.assertTrue(self.report()['qualified'])
        record=next((self.directory/'records').glob('*/unit.json'))
        duplicate=self.directory/'records/duplicate';duplicate.mkdir();shutil.copyfile(record,duplicate/'unit.json')
        with self.assertRaises(ValueError):self.report()
        shutil.rmtree(duplicate);self.assertTrue(self.report()['qualified'])

    def test_launcher_refuses_an_alias_even_when_it_points_to_the_same_binary(self):
        self.prepare()
        alias=self.root/'different-target-gcc';alias.symlink_to(self.cc)
        result=subprocess.run([str(self.directory/'bin/gcc-launcher'),str(alias),'-c',str(self.unit),'-o',str(self.obj)],
                              cwd=self.root,capture_output=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'category=compiler-identity',result.stderr)
        self.assertFalse(self.obj.exists())
        self.assertFalse(list((self.directory/'records').glob('*/unit.json')))

    def test_compiler_failure_cannot_create_a_success_record(self):
        self.prepare();self.header.unlink()
        self.assertNotEqual(self.compile().returncode,0)
        self.assertFalse(list((self.directory/'records').glob('*/unit.json')))
        with self.assertRaises(ValueError):self.report()

    def test_source_change_between_probe_and_actual_compile_is_refused(self):
        proxy=self.root/'compiler'
        proxy.write_text('#!'+sys.executable+'\nimport pathlib,subprocess,sys\n'
            'args=sys.argv[1:]\n'
            'if "-c" in args and "-M" not in args:\n'
            ' pathlib.Path('+repr(str(self.header))+').write_text("#define FIXTURE_VALUE 99\\n")\n'
            'raise SystemExit(subprocess.run(['+repr(self.cc)+',*args]).returncode)\n')
        proxy.chmod(0o700);self.prepare(proxy)
        result=self.compile();self.assertNotEqual(result.returncode,0)
        self.assertIn(b'category=source-mutation',result.stderr)
        self.assertNotIn(str(self.root).encode(),result.stderr)
        self.assertTrue(self.obj.is_file(),'the actual compiler ran before the input change was refused')
        self.assertFalse(list((self.directory/'records').glob('*/unit.json')))

    def test_post_probe_object_corruption_is_refused_without_a_success_record(self):
        proxy=self.root/'compiler'
        proxy.write_text('#!'+sys.executable+'\nimport pathlib,subprocess,sys\n'
            'status=subprocess.run(['+repr(self.cc)+',*sys.argv[1:]]).returncode\n'
            'obj=pathlib.Path('+repr(str(self.obj))+')\n'
            'if "-M" in sys.argv and obj.exists():obj.write_bytes(b"")\n'
            'raise SystemExit(status)\n')
        proxy.chmod(0o700);self.prepare(proxy)
        result=self.compile()
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'category=object',result.stderr)
        self.assertFalse(list((self.directory/'records').glob('*/unit.json')))

    def test_changed_compiler_is_refused_before_execution(self):
        proxy=self.root/'compiler';proxy.write_text('#!'+sys.executable+'\nraise SystemExit(0)\n');proxy.chmod(0o700)
        self.prepare(proxy);proxy.write_text(proxy.read_text()+'# changed identity\n')
        self.assertNotEqual(self.compile().returncode,0)
        self.assertFalse(self.obj.exists())

    def test_cycles_and_ambiguous_compile_inputs_fail_closed(self):
        response=self.root/'cycle';response.write_text('@'+str(response))
        with self.assertRaises(ValueError):self.capture.expanded(['@'+str(response)],self.root)
        with self.assertRaises(ValueError):self.capture.compile_paths(['-c','a.c','b.c','-o','x.o'],self.root)
        with self.assertRaises(ValueError):self.capture.compile_paths(['-c','a.c'],self.root)
        with self.assertRaises(ValueError):self.capture.dependencies(self.header,self.root)


if __name__=='__main__':unittest.main()
