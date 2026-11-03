//! Pinned numerical adapter build. Rust owns sequencing and execution constraints.
use nxrs_build::{output, read, Result};
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};
#[path = "graph.rs"]
mod graph;
pub fn qualify_context(header: &Path) -> Result<()> {
    let config = read(&header.join("nuttx/config.h"))?;
    let enabled = |name: &str| regex_match(&config, &format!(r"(?m)^\s*#\s*define\s+{name}\s+1\b"));
    if !enabled("CONFIG_ARCH_CHIP_ESP32S3")
        || !enabled("CONFIG_BUILD_FLAT")
        || (enabled("CONFIG_SMP")
            && ([
                "CONFIG_ARCH_PERF_EVENTS",
                "CONFIG_TIMER_ARCH",
                "CONFIG_ALARM_ARCH",
                "CONFIG_PERF_OVERFLOW_CORRECTION",
            ]
            .iter()
            .any(|n| enabled(n))
                || !regex_match(&config, r"(?m)^\s*#\s*define\s+CONFIG_SMP_NCPUS\s+2\b")))
    {
        return Err("PICO context requires ESP32-S3 flat NuttX with one/two CPUs and an SMP-safe perf clock".into());
    }
    Ok(())
}
fn regex_match(text: &str, pattern: &str) -> bool {
    nxrs_build::regex::Regex::new(pattern)
        .expect("static build pattern")
        .is_match(text)
}
fn entries(data: &[u8]) -> Result<()> {
    fn number(data: &[u8], p: usize, n: usize) -> Result<usize> {
        let bytes = data
            .get(p..p.checked_add(n).ok_or("ELF offset overflow")?)
            .ok_or("truncated assembly ELF")?;
        Ok(bytes
            .iter()
            .enumerate()
            .map(|(i, b)| (*b as usize) << (8 * i))
            .sum())
    }
    if data.get(..6) != Some(b"\x7fELF\x01\x01") || number(data, 18, 2)? != 94 {
        return Err("expected little-endian Xtensa ELF32".into());
    }
    let offset = number(data, 32, 4)?;
    let stride = number(data, 46, 2)?;
    let count = number(data, 48, 2)?;
    if stride < 40 {
        return Err("invalid section stride".into());
    }
    let mut sections = vec![];
    for i in 0..count {
        let mut section = [0; 10];
        for (j, v) in section.iter_mut().enumerate() {
            *v = number(data, offset + i * stride + j * 4, 4)?;
        }
        sections.push(section);
    }
    for section in &sections {
        if section[1] != 2 {
            continue;
        }
        if section[9] < 16 {
            return Err("invalid symbol stride".into());
        }
        for p in (section[4]
            ..section[4]
                .checked_add(section[5])
                .ok_or("ELF size overflow")?)
            .step_by(section[9])
        {
            let info = number(data, p + 12, 1)?;
            let index = number(data, p + 14, 2)?;
            if info != 0x12 || index == 0 {
                continue;
            }
            let section = sections.get(index).ok_or("invalid symbol section")?;
            let p = section[4] + number(data, p + 4, 4)?;
            let instruction = data.get(p..p + 3).ok_or("truncated SDK instruction")?;
            if instruction[0] != 0x36 || instruction[1] & 15 != 1 {
                return Err("SDK assembly function does not begin with ENTRY".into());
            }
        }
    }
    Ok(())
}
pub fn build(root: &Path, sdk: &Path, out: &Path, target: &str) -> Result<()> {
    nxrs_build::source::verify_inventory(&root.join("upstream/esp-dl/source.json"), sdk)?;
    let optimized = target.starts_with("xtensa-esp32s3-");
    if target.contains("nuttx") && !optimized {
        return Err("PICO backend is not qualified for this NuttX CPU".into());
    }
    let model = sdk.join("models/pedestrian_detect/models/s3/pedestrian_detect_pico_s8_v1.espdl");
    fs::create_dir_all(out).map_err(|e| e.to_string())?;
    nxrs_build::write_json(
        &out.join("graph.json"),
        &graph::generate(
            &fs::read(model).map_err(|e| e.to_string())?,
            &out.join("engine.cpp"),
            optimized,
        )?,
    )?;
    let port = out.join("port");
    fs::create_dir_all(&port).map_err(|e| e.to_string())?;
    for name in [
        "esp_err.h",
        "esp_heap_caps.h",
        "esp_cpu.h",
        "esp_log.h",
        "esp_memory_utils.h",
        "esp_mmu_map.h",
        "esp_system.h",
        "esp_timer.h",
        "freertos/FreeRTOS.h",
        "freertos/task.h",
        "soc/soc_caps.h",
        "esp32s3/rom/cache.h",
        "soc/extmem_reg.h",
    ] {
        let path = port.join(name);
        fs::create_dir_all(path.parent().unwrap()).map_err(|e| e.to_string())?;
        fs::write(path, "#include \"compat.hpp\"\n").map_err(|e| e.to_string())?;
    }
    fs::write(
        port.join("sdkconfig.h"),
        if optimized {
            "#define CONFIG_IDF_TARGET_ESP32S3 1\n#define CONFIG_SPIRAM 1\n"
        } else {
            ""
        },
    )
    .map_err(|e| e.to_string())?;
    fs::write(
        port.join("soc/soc_caps.h"),
        if optimized {
            "#include \"compat.hpp\"\n#define SOC_SIMD_INSTRUCTION_SUPPORTED 1\n"
        } else {
            "#include \"compat.hpp\"\n"
        },
    )
    .map_err(|e| e.to_string())?;
    let base = sdk.join("esp-dl");
    let mut includes = vec![port, root.join("hal/detector/src/compute/esp_dl/ffi")];
    includes.extend(
        [
            "dl",
            "dl/tool/include",
            "dl/tensor/include",
            "dl/base",
            "dl/base/isa",
            "dl/math/include",
            "dl/model/include",
            "dl/module/include",
            "fbs_loader/include",
        ]
        .iter()
        .map(|p| base.join(p)),
    );
    let mut sources = vec![out.join("engine.cpp")];
    sources.extend(
        [
            "dl/tool/src/dl_tool.cpp",
            "dl/tensor/src/dl_tensor_base.cpp",
            "dl/model/src/dl_model_context.cpp",
            "dl/module/src/dl_module_base.cpp",
        ]
        .iter()
        .map(|p| base.join(p)),
    );
    sources.extend(
        [
            "conv2d",
            "depthwise_conv2d",
            "mul",
            "mul2d",
            "avg_pool2d",
            "resize",
            "pad",
            "requantize_linear",
            "elemwise",
            "shape",
        ]
        .iter()
        .map(|n| base.join(format!("dl/base/dl_base_{n}.cpp"))),
    );
    let compiler = env::var("CXX").unwrap_or_else(|_| {
        if optimized {
            "xtensa-esp32s3-elf-g++"
        } else {
            "c++"
        }
        .into()
    });
    let archive = env::var("AR").unwrap_or_else(|_| {
        if optimized {
            "xtensa-esp32s3-elf-ar"
        } else {
            "ar"
        }
        .into()
    });
    let mut flags: Vec<String> = [
        "-std=gnu++17",
        "-O3",
        "-ffunction-sections",
        "-fdata-sections",
        "-fno-exceptions",
        "-fno-rtti",
        "-fno-threadsafe-statics",
    ]
    .map(str::to_owned)
    .into();
    if optimized {
        let header = PathBuf::from(
            env::var_os("NXRS_NUTTX_INCLUDE")
                .ok_or("native build requires configured NuttX headers")?,
        );
        qualify_context(&header)?;
        includes.push(header);
        flags.extend(["-mlongcalls".into(), "-mtext-section-literals".into()]);
        for p in [
            "dl/base/isa/tie728",
            "dl/base/isa/xtensa",
            "dl/tool/isa/tie728",
            "dl/tool/isa/xtensa",
        ] {
            includes.push(base.join(p));
        }
        for p in [
            "dl/base/isa/tie728",
            "dl/tool/isa/tie728",
            "dl/tool/isa/xtensa",
        ] {
            let mut assembly = vec![];
            for entry in fs::read_dir(base.join(p)).map_err(|e| e.to_string())? {
                let p = entry.map_err(|e| e.to_string())?.path();
                if p.extension().is_some_and(|s| s == "S") {
                    assembly.push(p);
                }
            }
            assembly.sort();
            sources.extend(assembly);
        }
    }
    flags.extend(includes.iter().map(|p| format!("-I{}", p.display())));
    let objects: Vec<_> = (0..sources.len())
        .map(|i| out.join(format!("{i}.o")))
        .collect();
    // Two bounded compiler workers, joined even on failure before any archive is published.
    std::thread::scope(|scope| -> Result<()> {
        let mut workers = vec![];
        for begin in (0..sources.len()).step_by(sources.len().div_ceil(2)) {
            let end = (begin + sources.len().div_ceil(2)).min(sources.len());
            let (sources, objects, flags, compiler) = (&sources, &objects, &flags, &compiler);
            workers.push(scope.spawn(move || -> Result<()> {
                for i in begin..end {
                    let assembly = sources[i].extension().is_some_and(|s| s == "S");
                    let flags: Vec<_> = flags
                        .iter()
                        .filter(|s| !assembly || s.as_str() != "-mtext-section-literals")
                        .collect();
                    let mut cmd = Command::new(compiler);
                    cmd.args(flags);
                    if optimized && assembly {
                        cmd.arg("-mno-text-section-literals");
                    }
                    output(cmd.arg("-c").arg(&sources[i]).arg("-o").arg(&objects[i]))?;
                    if optimized && assembly {
                        entries(&fs::read(&objects[i]).map_err(|e| e.to_string())?)?;
                    }
                }
                Ok(())
            }));
        }
        let mut result = Ok(());
        for worker in workers {
            let r = worker
                .join()
                .map_err(|_| "compiler worker panicked".to_owned())
                .and_then(|r| r);
            if result.is_ok() {
                result = r;
            }
        }
        result
    })?;
    let temp = nxrs_build::TempDir::new(out)?;
    let fresh = temp.0.join("libpico.a");
    output(Command::new(archive).arg("crs").arg(&fresh).args(objects))?;
    fs::rename(fresh, out.join("libpico.a")).map_err(|e| e.to_string())?;
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=pico");
    if optimized {
        for (flag, library) in [
            ("-print-file-name=libstdc++.a", "stdc++"),
            ("-print-libgcc-file-name", "gcc"),
        ] {
            let path = output(Command::new(&compiler).arg(flag))?;
            println!(
                "cargo:rustc-link-search=native={}",
                Path::new(path.trim())
                    .parent()
                    .ok_or("compiler runtime path missing")?
                    .display()
            );
            println!("cargo:rustc-link-lib=static={library}");
        }
    } else {
        println!(
            "cargo:rustc-link-lib=dylib={}",
            if target.contains("apple") {
                "c++"
            } else {
                "stdc++"
            }
        );
    }
    Ok(())
}
