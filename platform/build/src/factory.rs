//! Offline protected-factory packaging. Uses vendor cryptographic operations; never provisions a device.
use crate::{
    boot::{self, Layout},
    hash, hash_bytes, output, output_bytes,
    profile::Profile,
    read_json, write_json, Result, TempDir,
};
use serde_json::json;
use std::{fs, path::Path, process::Command};
fn number(bytes: &[u8], p: usize, n: usize) -> Result<usize> {
    let data = bytes
        .get(p..p.checked_add(n).ok_or("image offset overflow")?)
        .ok_or("truncated signed image")?;
    Ok(data
        .iter()
        .enumerate()
        .map(|(i, b)| (*b as usize) << (8 * i))
        .sum())
}
fn protected_identity(image: &[u8], class: &[u8; 16]) -> Result<()> {
    if number(image, 0, 4)? != 0x96f3b83d {
        return Err("invalid MCUboot header".into());
    }
    let start = number(image, 8, 2)?
        .checked_add(number(image, 12, 4)?)
        .ok_or("image size overflow")?;
    let length = number(image, 10, 2)?;
    if length < 4 {
        return Err("factory image requires one protected 32-bit security counter".into());
    }
    if number(image, start, 2)? != 0x6908 || number(image, start + 2, 2)? != length {
        return Err("invalid protected TLV area".into());
    }
    let end = start
        .checked_add(length)
        .ok_or("protected TLV size overflow")?;
    let mut p = start + 4;
    let (mut counters, mut classes) = (0, 0);
    while p < end {
        let kind = number(image, p, 2)?;
        let n = number(image, p + 2, 2)?;
        p = p.checked_add(4).ok_or("TLV offset overflow")?;
        let next = p
            .checked_add(n)
            .filter(|n| *n <= end)
            .ok_or("invalid protected TLV boundary")?;
        let bytes = image.get(p..next).ok_or("truncated protected TLV")?;
        if kind == 0x50 {
            if n != 4 {
                return Err("factory image requires a protected 32-bit counter".into());
            }
            counters += 1;
        }
        if kind == 0x75 {
            if bytes != class {
                return Err("factory image class does not match product".into());
            }
            classes += 1;
        }
        p = next;
    }
    if p != end || counters != 1 {
        return Err("factory image requires one protected 32-bit security counter".into());
    }
    if classes != 1 {
        return Err("factory image class must be present exactly once".into());
    }
    Ok(())
}
pub struct Inputs<'a> {
    pub boot: &'a Path,
    pub application: &'a Path,
    pub rom_root: &'a Path,
    pub app_root: &'a Path,
    pub flash_key: &'a Path,
}
pub fn prepare(p: &Profile, input: Inputs<'_>, out: &Path) -> Result<()> {
    boot::application_config(p)?;
    let b = Layout::parse(&p.data["mcuboot"])?;
    if !boot::protected(p)? || out.exists() || out.symlink_metadata().is_ok() {
        return Err(
            "factory preparation requires a protected profile and fresh output directory".into(),
        );
    }
    if output(Command::new("imgtool").arg("version"))?.trim() != "2.2.0"
        || !output(Command::new("esptool.py").arg("version"))?
            .lines()
            .any(|s| s.trim() == "4.8.1")
    {
        return Err("factory preparation requires imgtool 2.2.0 and esptool 4.8.1".into());
    }
    let rom = boot::public_root(input.rom_root, true)?;
    let app = boot::public_root(input.app_root, false)?;
    let key = fs::read(input.flash_key).map_err(|e| e.to_string())?;
    if key.len() != 32 {
        return Err("XTS_AES_128_KEY must contain exactly 32 bytes".into());
    }
    let metadata = read_json(&input.boot.join("bootloader.json"))?;
    let expected = json!({"mcuboot":boot::MCUBOOT_REVISION,"esp-hal-3rdparty":boot::HAL_REVISION,"platform":p.name(),"layout":p.data["mcuboot"],"image_class":p.data["image-class"],"bootloader_file":"bootloader.signed.bin","hardware_secure_boot":true,"flash_encryption":true,"efuse_writes_forbidden":true,"encrypted_write_once":true,"rom_root_pem_sha256":rom.source_sha256,"public_root_sha256":hash_bytes(&app.pem)});
    if expected
        .as_object()
        .unwrap()
        .iter()
        .any(|(key, value)| metadata.get(key) != Some(value))
    {
        return Err("boot build does not match protected profile and expected roots".into());
    }
    let boot = fs::read(input.boot.join("bootloader.signed.bin")).map_err(|e| e.to_string())?;
    let image = fs::read(input.application).map_err(|e| e.to_string())?;
    if boot.is_empty()
        || boot.len() > 0xf000
        || boot.len() % 32 != 0
        || metadata["bootloader_sha256"] != hash_bytes(&boot)
    {
        return Err("bootloader size/alignment/build digest mismatch".into());
    }
    if image.len() != b.slot as usize {
        return Err("factory application must be a full confirmed slot image".into());
    }
    // The pinned SDK owns trailer layout. This is a single vendor API operation;
    // admission, ownership, bounds, encryption sequencing and publication stay in Rust.
    let trailer=output_bytes(Command::new("python3").args(["-c","from imgtool.image import Image; import sys; i=Image(align=32,max_align=32,max_sectors=int(sys.argv[1]),confirm=True); i.payload=bytearray(); i.pad_to(i._trailer_size(32,i.max_sectors,False,None,False,0)); sys.stdout.buffer.write(i.payload)",&(b.slot/b.erase).to_string()]))?;
    if trailer.is_empty() || trailer.len() > image.len() || !image.ends_with(&trailer) {
        return Err("factory image needs a fresh upstream confirmed trailer".into());
    }
    protected_identity(&image, &boot::image_class(p)?)?;
    let parent = out.parent().ok_or("factory output needs a parent")?;
    let temporary = TempDir::new(parent)?;
    let work = &temporary.0;
    let bundle = work.join("bundle");
    fs::create_dir(&bundle).map_err(|e| e.to_string())?;
    for (name, bytes) in [
        ("rom.pem", rom.pem.as_slice()),
        ("app.pem", app.pem.as_slice()),
        ("key.bin", key.as_slice()),
        ("boot.bin", boot.as_slice()),
        ("primary.bin", image.as_slice()),
    ] {
        fs::write(work.join(name), bytes).map_err(|e| e.to_string())?;
    }
    output(
        Command::new("espsecure.py")
            .args(["verify_signature", "--version", "2", "--keyfile"])
            .arg(work.join("rom.pem"))
            .arg(work.join("boot.bin")),
    )?;
    output(
        Command::new("imgtool")
            .args(["verify", "-k"])
            .arg(work.join("app.pem"))
            .arg(work.join("primary.bin")),
    )?;
    let empty_slot = vec![0xff; b.slot as usize];
    let scratch = vec![0xff; b.scratch_size as usize];
    let mut regions = vec![];
    for (name, offset, plain) in [
        ("boot", 0, boot.as_slice()),
        ("primary", b.primary, image.as_slice()),
        ("secondary", b.secondary, empty_slot.as_slice()),
        ("scratch", b.scratch, scratch.as_slice()),
    ] {
        let source = work.join(format!("{name}.bin"));
        fs::write(&source, plain).map_err(|e| e.to_string())?;
        let encrypted = bundle.join(format!("{name}.enc.bin"));
        let decoded = work.join("decoded.bin");
        let operation = |name: &str, source: &Path, destination: &Path| -> Result<()> {
            output(
                Command::new("espsecure.py")
                    .args([name, "--aes_xts", "--keyfile"])
                    .arg(work.join("key.bin"))
                    .args(["--address", &format!("{offset:#x}"), "--output"])
                    .arg(destination)
                    .arg(source),
            )
            .map(|_| ())
        };
        operation("encrypt_flash_data", &source, &encrypted)?;
        let mut ciphertext = fs::read(&encrypted).map_err(|e| e.to_string())?;
        if ciphertext.len() != plain.len() {
            return Err("vendor ciphertext size mismatch".into());
        }
        let start = match name {
            "boot" => plain.len(),
            "primary" => image.len() - trailer.len(),
            _ => 0,
        };
        for position in (start..plain.len()).step_by(16) {
            if plain[position..position + 16].iter().all(|b| *b == 0xff) {
                ciphertext[position..position + 16].fill(0xff);
            }
        }
        fs::write(&encrypted, &ciphertext).map_err(|e| e.to_string())?;
        operation("decrypt_flash_data", &encrypted, &decoded)?;
        let mut actual = fs::read(&decoded).map_err(|e| e.to_string())?;
        if actual.len() != plain.len() {
            return Err("vendor plaintext size mismatch".into());
        }
        for position in (0..actual.len()).step_by(16) {
            if ciphertext[position..position + 16]
                .iter()
                .all(|b| *b == 0xff)
            {
                if position < start {
                    return Err(
                        "executable ciphertext collides with erased AES-block marker".into(),
                    );
                }
                actual[position..position + 16].fill(0xff);
            }
        }
        if actual != plain {
            return Err("address-specific SDK ciphertext round-trip failed".into());
        }
        regions.push(json!({"file":encrypted.file_name().unwrap().to_string_lossy(),"offset":offset,"size":ciphertext.len(),"sha256":hash(&encrypted)?}));
    }
    write_json(
        &bundle.join("factory.json"),
        &json!({"platform":p.name(),"layout":p.data["mcuboot"],"regions":regions,"image_class":p.data["image-class"],"rom_root_pem_sha256":rom.source_sha256,"app_root_pem_sha256":hash_bytes(&app.pem),"flash_encryption":"xts-aes-128","efuse_writes_forbidden":true,"encrypted_write_once":true,"hardware_qualified":false,"runtime_updates":false,"requires_factory_provisioning":true}),
    )?;
    fs::rename(bundle, out).map_err(|e| e.to_string())
}
