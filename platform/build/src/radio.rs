//! Hash-pinned Pico radio assets. Generated blobs never enter source control.
use crate::{fetch_bounded, hash, read, write_json, Result, TempDir};
use serde_json::json;
use std::{
    env, fs,
    path::{Path, PathBuf},
};
const LICENSE: &str = "67aca4f10d9edf489871f64cd8f0dcd6c5df3e4ce75bd39e1914fc54f99e40b3";
fn bytes(text: &str, name: &str) -> Result<Vec<u8>> {
    let pattern = format!(
        r"(?s)unsigned char {}\[\] CYW43_RESOURCE_ATTRIBUTE = \{{(.*?)\n\}};",
        regex::escape(name)
    );
    let re = regex::Regex::new(&pattern).map_err(|e| e.to_string())?;
    let array = re
        .captures(text)
        .ok_or("missing CYW43439 byte array")?
        .get(1)
        .unwrap()
        .as_str();
    if !regex::Regex::new(r"^\s*0x[0-9a-fA-F]{2}(?:\s*,\s*0x[0-9a-fA-F]{2})*\s*,?\s*$")
        .unwrap()
        .is_match(array)
    {
        return Err("unexpected radio array syntax".into());
    }
    array
        .trim()
        .trim_end_matches(',')
        .split(',')
        .map(|v| {
            u8::from_str_radix(v.trim().trim_start_matches("0x"), 16).map_err(|e| e.to_string())
        })
        .collect()
}
fn wifi(text: &str, name: &str, length: usize) -> Result<Vec<u8>> {
    let bytes = bytes(text, name)?;
    for (name, expected) in [("CYW43_WIFI_FW_LEN", length), ("CYW43_CLM_LEN", 984)] {
        let re = regex::Regex::new(&format!(r"(?m)^#define {name} \((\d+)\)")).unwrap();
        if re.captures(text).and_then(|c| c[1].parse::<usize>().ok()) != Some(expected) {
            return Err(format!("unexpected {name}"));
        }
    }
    let clm = (length + 511) & !511;
    if bytes.len() != clm + 984 || bytes[length..clm].iter().any(|b| *b != 0) {
        return Err("invalid firmware/CLM boundary or padding".into());
    }
    Ok(bytes)
}
fn bluetooth(text: &str, length: usize) -> Result<Vec<u8>> {
    let bytes = bytes(text, "cyw43_btfw_43439")?;
    if bytes.len() != length || bytes.is_empty() {
        return Err("unexpected Bluetooth binary length".into());
    }
    let count = bytes[0] as usize + 1;
    if count < 2
        || count >= bytes.len()
        || bytes[count - 1] != 0
        || bytes[1..count - 1].iter().any(|b| *b == 0 || !b.is_ascii())
    {
        return Err("invalid Bluetooth version string".into());
    }
    let mut cursor = count + 1;
    while cursor < bytes.len() {
        if bytes.len() - cursor < 4 {
            return Err("truncated Bluetooth record".into());
        }
        let n = bytes[cursor] as usize;
        let address = u16::from_be_bytes([bytes[cursor + 1], bytes[cursor + 2]]);
        let kind = bytes[cursor + 3];
        cursor += 4;
        if !(match kind {
            0 => n > 0,
            1 => n == 0,
            2 | 4 => n == 2,
            5 => n == 4,
            _ => false,
        }) || n > bytes.len() - cursor
        {
            return Err("invalid Bluetooth record".into());
        }
        cursor += n;
        if kind == 1 {
            if address != 0 || bytes[cursor..].iter().any(|b| *b != 0) {
                return Err("invalid Bluetooth EOF/padding".into());
            }
            return Ok(bytes);
        }
    }
    Err("missing Bluetooth EOF record".into())
}
fn atomic(path: &Path, data: &[u8]) -> Result<()> {
    let parent = path.parent().ok_or("asset parent missing")?;
    let tmp = TempDir::new(parent)?;
    let file = tmp.0.join("asset");
    fs::write(&file, data).map_err(|e| e.to_string())?;
    fs::rename(file, path).map_err(|e| e.to_string())
}
pub fn prepare(root: &Path, out: &Path, kind: &str) -> Result<(PathBuf, usize, Option<PathBuf>)> {
    prepare_cached(
        &root.join("target/radio-deps/cyw43439"),
        Some(&out.join("radio-firmware.json")),
        kind,
        &env::var("NXRS_PICO_RADIO_RELEASE").unwrap_or("pico-sdk-2.3.1".into()),
    )
}
pub fn prepare_cached(
    cache_base: &Path,
    record_path: Option<&Path>,
    kind: &str,
    release: &str,
) -> Result<(PathBuf, usize, Option<PathBuf>)> {
    let bt = match kind {
        "cyw43439" => false,
        "cyw43439-bluetooth" => true,
        _ => return Err("unsupported radio-firmware".into()),
    };
    let (sdk, revision, bt_hash, bt_size, bt_length) = match release {
        "pico-sdk-2.3.1" => (
            "2.3.1",
            "055d64274b014dd7b1c2fc94d26e8a18face7124",
            "d05a8dca7e232ada470d8ed723edaa1ea78000098870f9b979d490d83ad35b22",
            43299,
            6970,
        ),
        "pico-sdk-2.1.1" => (
            "2.1.1",
            "c1075d4bc440422cf2b2fd12c64a1f53f77660ee",
            "11fc817e33a875e4f619c6939f934e916be0dfa083ce4af073867f340779a6c9",
            38130,
            6164,
        ),
        _ => return Err("unknown radio asset release".into()),
    };
    let cache = cache_base.join(revision);
    let default_record = cache.join("firmware.json");
    let record_path = record_path.unwrap_or(&default_record);
    fs::create_dir_all(&cache).map_err(|e| e.to_string())?;
    let base = format!("https://raw.githubusercontent.com/georgerobotics/cyw43-driver/{revision}");
    let (name, digest, length) = if bt {
        (
            "wb43439A0_7_95_49_00_combined",
            "6b4b9a717c3c5d669b56a3bc38b58df3901f63e3a7363ccf5d74706460762176",
            231077,
        )
    } else {
        (
            "w43439A0_7_95_49_00_combined",
            "120bb63db1c5cf42cd44cf9a057d6f14601038a9413badf5a3b737f6552bc9ea",
            224190,
        )
    };
    let header = cache.join(format!("{name}.h"));
    let url = format!("{base}/firmware/{name}.h");
    fetch_bounded(&url, digest, 2_000_000, None, &header)?;
    fetch_bounded(
        &format!("{base}/LICENSE.RP"),
        LICENSE,
        2_000_000,
        None,
        &cache.join("LICENSE.RP"),
    )?;
    let binary = cache.join(if bt {
        "cyw43439-wb.bin"
    } else {
        "cyw43439.bin"
    });
    atomic(&binary, &wifi(&read(&header)?, name, length)?)?;
    let mut record = json!({"schema":1,"revision":revision,"sdk_release":sdk,"source":url,"header_sha256":digest,"binary":binary,"binary_sha256":hash(&binary)?,"wifi_length":length,"clm_offset":(length+511)&!511,"clm_length":984,"wifi_bluetooth":bt,"license":cache.join("LICENSE.RP"),"license_sha256":LICENSE});
    let bt_binary = if bt {
        let header = cache.join("cyw43_btfw_43439.h");
        let url = format!("{base}/firmware/cyw43_btfw_43439.h");
        fetch_bounded(&url, bt_hash, 2_000_000, Some(bt_size), &header)?;
        let blob = bluetooth(&read(&header)?, bt_length)?;
        let path = cache.join("cyw43439-bt.bin");
        atomic(&path, &blob)?;
        record["bluetooth"] = json!({"source":url,"header_sha256":bt_hash,"header_length":bt_size,"binary":path,"binary_sha256":hash(&path)?,"binary_length":blob.len(),"version":std::str::from_utf8(&blob[1..blob[0] as usize]).map_err(|e|e.to_string())?});
        Some(path)
    } else {
        None
    };
    fs::create_dir_all(record_path.parent().ok_or("radio record needs a parent")?)
        .map_err(|e| e.to_string())?;
    write_json(record_path, &record)?;
    Ok((binary, length, bt_binary))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn byte_arrays_are_strict() {
        assert_eq!(
            bytes(
                "unsigned char x[] CYW43_RESOURCE_ATTRIBUTE = { 0x01, 0xff,\n};",
                "x"
            )
            .unwrap(),
            [1, 255]
        );
        for value in ["0x1", "0xff;evil", "1", "0x100", ""] {
            assert!(bytes(
                &format!("unsigned char x[] CYW43_RESOURCE_ATTRIBUTE = {{ {value}\n}};"),
                "x"
            )
            .is_err());
        }
    }
}

#[cfg(test)]
mod protocol_tests {
    use super::*;
    fn header(data: &[u8], name: &str, wifi: Option<usize>) -> String {
        format!(
            "{}unsigned char {name}[] CYW43_RESOURCE_ATTRIBUTE = {{ {}\n}};\n{}",
            if wifi.is_some() {
                "static const "
            } else {
                "const "
            },
            data.iter()
                .map(|b| format!("0x{b:02x}"))
                .collect::<Vec<_>>()
                .join(","),
            wifi.map(|n| format!("#define CYW43_WIFI_FW_LEN ({n})\n#define CYW43_CLM_LEN (984)\n"))
                .unwrap_or_default()
        )
    }
    #[test]
    fn wifi_padding_lengths_and_header_protocol() {
        let n = 5;
        let mut data = vec![1; 512 + 984];
        data[n..512].fill(0);
        let valid = header(&data, "wifi", Some(n));
        assert_eq!(wifi(&valid, "wifi", n).unwrap(), data);
        assert!(wifi(&valid, "wifi", n + 1).is_err());
        let mut corrupt = data.clone();
        corrupt[n] = 1;
        assert!(wifi(&header(&corrupt, "wifi", Some(n)), "wifi", n).is_err());
        assert!(wifi(&header(&data[..data.len() - 1], "wifi", Some(n)), "wifi", n).is_err());
        assert!(wifi(&valid.replace("(984)", "(985)"), "wifi", n).is_err());
    }
    #[test]
    fn bluetooth_records_require_eof_and_complete_payload() {
        let valid = [2, b'v', 0, 1, 1, 0, 0, 0, 0x44, 0, 0, 0, 1, 0, 0];
        let source = header(&valid, "cyw43_btfw_43439", None);
        assert_eq!(bluetooth(&source, valid.len()).unwrap(), valid);
        for (index, value) in [(0, 0), (2, b'x'), (4, 0), (12, 3), (14, 1)] {
            let mut bad = valid;
            bad[index] = value;
            assert!(bluetooth(&header(&bad, "cyw43_btfw_43439", None), bad.len()).is_err());
        }
        assert!(bluetooth(&header(&valid[..10], "cyw43_btfw_43439", None), 10).is_err());
    }
}
