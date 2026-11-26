//! Emit the pinned upstream S8 dispatch tables for the fixed numerical graph.
use nxrs_build_support::{read_json, Result};
use std::{collections::BTreeSet, fs, path::Path};

pub fn generate(pin: &Path, out: &Path, optimized: bool) -> Result<()> {
    println!("cargo:rerun-if-changed={}", pin.display());
    let data = read_json(pin)?;
    if data["kernel_abi"].as_u64() != Some(1) {
        return Err("unsupported PICO kernel ABI".into());
    }
    let name = if optimized { "esp32s3" } else { "scalar" };
    let prefix = if optimized {
        "dl_tie728_s8_"
    } else {
        "dl_c_s8_"
    };
    let rows = data[name].as_array().ok_or("invalid PICO kernel table")?;
    if rows.is_empty() {
        return Err("empty PICO kernel table".into());
    }
    let mut previous = None;
    let mut symbols = BTreeSet::new();
    let mut select = String::from("// Pinned S8 rows from upstream spec/select/Conv.yml.\n");
    for row in rows {
        let row = row
            .as_array()
            .filter(|r| r.len() == 4)
            .ok_or("invalid PICO kernel row")?;
        let key = row[0].as_u64().ok_or("invalid PICO kernel key")?;
        if key > 1023 || key & 3 != 0 || previous.is_some_and(|p| p >= key) {
            return Err("PICO kernel keys must be unique, ordered and S8".into());
        }
        previous = Some(key);
        let mut slots = vec![];
        for (i, value) in row[1..].iter().enumerate() {
            if value.is_null() && i != 0 && (i != 2 || optimized) {
                slots.push("nullptr".into());
                continue;
            }
            let symbol = value.as_str().ok_or("missing PICO kernel slot")?;
            if !symbol.starts_with(prefix)
                || !symbol
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_')
            {
                return Err("invalid PICO kernel symbol".into());
            }
            symbols.insert(symbol);
            slots.push(format!("\"{symbol}\""));
        }
        select.push_str(&format!("    {{0x{key:04x}, {}}},\n", slots.join(", ")));
    }
    let mut header = String::from("#pragma once\n#define DL_COMPILE_ALL 0\n");
    let mut table = String::new();
    for symbol in symbols {
        header.push_str(&format!(
            "#define DL_KERNEL_{} 1\n",
            symbol.to_ascii_uppercase()
        ));
        table.push_str(&format!(
            "    {{\"{symbol}\", (dl_kernel_erased_t)({symbol})}},\n"
        ));
    }
    for (name, data) in [
        ("dl_compile_config.h", header),
        ("dl_kernel.inc", table),
        ("dl_conv_select.inc", select),
    ] {
        fs::write(out.join(name), data).map_err(|e| e.to_string())?;
    }
    Ok(())
}
