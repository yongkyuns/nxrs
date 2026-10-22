#!/usr/bin/env python3
"""Build only the fixed PICO graph and SDK numerical operators."""
import argparse, concurrent.futures, hashlib, importlib.util, json, os, pathlib, re, subprocess
ROOT=pathlib.Path(__file__).resolve().parents[6]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def qualify_compute_context(header):
    config = (pathlib.Path(header)/'nuttx/config.h').read_text()
    enabled = lambda name: re.search(r'^\s*#\s*define\s+'+name+r'\s+1\b', config, re.M) is not None
    if not enabled('CONFIG_ARCH_CHIP_ESP32S3') or not enabled('CONFIG_BUILD_FLAT') or enabled('CONFIG_SMP'):
        raise ValueError('PICO compute-context lease requires the qualified ESP32-S3 single-core flat NuttX profile')

def build(sdk,out,target):
    module('prepare',ROOT/'upstream/esp-dl/prepare.py').verify(sdk)
    optimized=target.startswith('xtensa-esp32s3-')
    if 'nuttx' in target and not optimized:raise ValueError('PICO numerical backend not qualified for this NuttX CPU')
    model=sdk/'models/pedestrian_detect/models/s3/pedestrian_detect_pico_s8_v1.espdl'
    out.mkdir(parents=True,exist_ok=True)
    stats=module('graph',ROOT/'upstream/esp-dl/graph.py').generate(model.read_bytes(),out/'engine.cpp',optimized)
    (out/'graph.json').write_text(json.dumps(stats,indent=2)+'\n')
    port=out/'port';port.mkdir(exist_ok=True)
    for name in ['esp_err.h','esp_heap_caps.h','esp_cpu.h','esp_log.h','esp_memory_utils.h','esp_mmu_map.h','esp_system.h','esp_timer.h','freertos/FreeRTOS.h','freertos/task.h','soc/soc_caps.h','esp32s3/rom/cache.h','soc/extmem_reg.h']:
        p=port/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('#include "compat.hpp"\n')
    (port/'sdkconfig.h').write_text('#define CONFIG_IDF_TARGET_ESP32S3 1\n#define CONFIG_SPIRAM 1\n' if optimized else '')
    (port/'soc/soc_caps.h').write_text('#include "compat.hpp"\n#define SOC_SIMD_INSTRUCTION_SUPPORTED 1\n' if optimized else '#include "compat.hpp"\n')
    base=sdk/'esp-dl'
    includes=[port,ROOT/'hal/detector/src/compute/esp_dl/ffi']+[base/p for p in ['dl','dl/tool/include','dl/tensor/include','dl/base','dl/base/isa','dl/math/include','dl/model/include','dl/module/include','fbs_loader/include']]
    names=['dl/tool/src/dl_tool.cpp','dl/tensor/src/dl_tensor_base.cpp','dl/model/src/dl_model_context.cpp','dl/module/src/dl_module_base.cpp']
    names+=['dl/base/dl_base_'+p+'.cpp' for p in ['conv2d','depthwise_conv2d','mul','mul2d','avg_pool2d','resize','pad','requantize_linear','elemwise','shape']]
    sources=[out/'engine.cpp']+[base/p for p in names]
    compiler=os.environ.get('CXX','xtensa-esp32s3-elf-g++' if optimized else 'c++')
    archive=os.environ.get('AR','xtensa-esp32s3-elf-ar' if optimized else 'ar')
    flags=['-std=gnu++17','-O3','-ffunction-sections','-fdata-sections','-fno-exceptions','-fno-rtti','-fno-threadsafe-statics']
    if optimized:
        header=os.environ.get('NXRS_NUTTX_INCLUDE')
        if not header:raise ValueError('native factory build must supply NXRS_NUTTX_INCLUDE')
        qualify_compute_context(header)
        includes.append(pathlib.Path(header))
        flags+=['-mlongcalls','-mtext-section-literals']
        for p in ['dl/base/isa/tie728','dl/base/isa/xtensa','dl/tool/isa/tie728','dl/tool/isa/xtensa']:includes.append(base/p)
        for p in ['dl/base/isa/tie728','dl/tool/isa/tie728','dl/tool/isa/xtensa']:sources+=sorted((base/p).glob('*.S'))
    flags+=['-I'+str(p) for p in includes]
    def compile_source(item):
        i,source=item;dest=out/f'{i}.o'
        # SDK assembly labels precede ENTRY; inline literal pools would make
        # their function addresses point at data. Keep assembly literals in
        # separate sections for NuttX's linker, as required by this SDK.
        source_flags = [flag for flag in flags if source.suffix != '.S' or flag != '-mtext-section-literals']
        if optimized and source.suffix == '.S':
            source_flags.append('-mno-text-section-literals')
        subprocess.run([compiler,*source_flags,'-c',str(source),'-o',str(dest)],check=True,capture_output=True)
        if optimized and source.suffix == '.S':
            module('qualify', ROOT/'upstream/esp-dl/qualify.py').check_entries(dest.read_bytes())
        return dest
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        try:objects=list(pool.map(compile_source,enumerate(sources)))
        except subprocess.CalledProcessError as e:
            raise RuntimeError(e.stderr.decode(errors='replace')) from e
    subprocess.run([archive,'crs',str(out/'libpico.a'),*map(str,objects)],check=True)
    print('cargo:rustc-link-search=native='+str(out))
    print('cargo:rustc-link-lib=static=pico')
    if optimized:
        library=subprocess.check_output([compiler,'-print-file-name=libstdc++.a'],text=True).strip()
        print('cargo:rustc-link-search=native='+str(pathlib.Path(library).parent))
        print('cargo:rustc-link-lib=static=stdc++')
        gcc=subprocess.check_output([compiler,'-print-libgcc-file-name'],text=True).strip()
        print('cargo:rustc-link-search=native='+str(pathlib.Path(gcc).parent))
        print('cargo:rustc-link-lib=static=gcc')
    else:print('cargo:rustc-link-lib=dylib='+('c++' if 'apple' in target else 'stdc++'))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--sdk',type=pathlib.Path,required=True);p.add_argument('--out',type=pathlib.Path,required=True);p.add_argument('--target',required=True);a=p.parse_args();build(a.sdk,a.out,a.target)
