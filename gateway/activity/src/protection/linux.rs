//! Kernel-observed mount and direct dm-crypt identity. No shell or privileged ioctl.
use super::Volume;
use crate::store::Error;
use std::{fs, io::Read, os::unix::fs::MetadataExt, path::Path};

fn bounded(path: &Path, max: u64) -> Result<String, Error> {
    let mut bytes = Vec::new();
    fs::File::open(path)
        .map_err(|_| Error::Protection)?
        .take(max + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| Error::Protection)?;
    if bytes.len() as u64 > max {
        return Err(Error::Protection);
    }
    String::from_utf8(bytes).map_err(|_| Error::Protection)
}

fn mount_device<'a>(table: &'a str, volume: &Volume) -> Result<&'a str, Error> {
    let mut device = None;
    let mut rows = Vec::new();
    for line in table.lines() {
        let (left, right) = line.split_once(" - ").ok_or(Error::Protection)?;
        let left: Vec<_> = left.split_whitespace().collect();
        let right: Vec<_> = right.split_whitespace().collect();
        if left.len() < 6 || right.len() < 3 {
            return Err(Error::Protection);
        }
        // The declared path is restricted to literal ASCII without mountinfo escapes.
        // Decode only kernel-defined escapes for containment checks on other mounts.
        let path = left[4]
            .replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\012", "\n")
            .replace("\\134", "\\");
        if Path::new(&path).starts_with(&volume.mount) {
            let hardened = ["rw", "nosuid", "nodev", "noexec"]
                .iter()
                .all(|flag| left[5].split(',').any(|value| value == *flag));
            if right[0] != "ext4" || !hardened {
                return Err(Error::Protection);
            }
            if Path::new(&path) == volume.mount
                && (left[3] != "/" || device.replace(left[2]).is_some())
            {
                return Err(Error::Protection);
            }
            rows.push(left[2]);
        }
    }
    let device = device.ok_or(Error::Protection)?;
    // ReadWritePaths may create bind mounts of this same encrypted filesystem.
    // A nested mount of any other filesystem/device would expose plaintext.
    if rows.iter().any(|value| *value != device) {
        return Err(Error::Protection);
    }
    let (major, minor) = device.split_once(':').ok_or(Error::Protection)?;
    if major.is_empty()
        || minor.is_empty()
        || !major
            .bytes()
            .chain(minor.bytes())
            .all(|b| b.is_ascii_digit())
    {
        return Err(Error::Protection);
    }
    Ok(device)
}

fn identity(identity: &str, volume: &Volume) -> Result<(), Error> {
    let prefix = format!("CRYPT-LUKS2-{}-", volume.luks_uuid.replace('-', ""));
    let name = identity
        .trim_end_matches('\n')
        .strip_prefix(&prefix)
        .ok_or(Error::Protection)?;
    if name.is_empty()
        || name.len() > 128
        || !name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_+.-".contains(&b))
    {
        return Err(Error::Protection);
    }
    Ok(())
}

pub(super) fn verify(volume: &Volume) -> Result<(), Error> {
    if fs::canonicalize(&volume.mount).map_err(|_| Error::Protection)? != volume.mount {
        return Err(Error::Protection);
    }
    let metadata = fs::symlink_metadata(&volume.mount).map_err(|_| Error::Protection)?;
    if !metadata.is_dir() || metadata.uid() != 0 || metadata.mode() & 0o022 != 0 {
        return Err(Error::Protection);
    }
    let table = bounded(Path::new("/proc/self/mountinfo"), 512 * 1024)?;
    let device = mount_device(&table, volume)?;
    if device
        != format!(
            "{}:{}",
            libc::major(metadata.dev()),
            libc::minor(metadata.dev())
        )
    {
        return Err(Error::Protection);
    }
    identity(
        &bounded(
            &Path::new("/sys/dev/block").join(device).join("dm/uuid"),
            256,
        )?,
        volume,
    )
}

pub(super) fn operator_uid() -> Result<u32, Error> {
    let status = bounded(Path::new("/proc/self/status"), 65536)?;
    let fields: Vec<_> = status
        .lines()
        .find_map(|line| line.strip_prefix("Uid:"))
        .ok_or(Error::Protection)?
        .split_whitespace()
        .map(|value| value.parse::<u32>())
        .collect::<Result<_, _>>()
        .map_err(|_| Error::Protection)?;
    if fields.len() != 4 || fields[0] == 0 || fields.iter().any(|value| *value != fields[0]) {
        return Err(Error::Protection);
    }
    for name in ["CapEff:", "CapPrm:", "CapAmb:"] {
        let value = status
            .lines()
            .find_map(|line| line.strip_prefix(name))
            .ok_or(Error::Protection)?
            .trim();
        if u64::from_str_radix(value, 16).map_err(|_| Error::Protection)? != 0 {
            return Err(Error::Protection);
        }
    }
    Ok(fields[0])
}

#[cfg(test)]
mod tests {
    use super::*;
    fn volume() -> Volume {
        Volume {
            layout_version: 2,
            mount: "/var/lib/nxrs-encrypted".into(),
            luks_uuid: "01234567-89ab-cdef-0123-456789abcdef".into(),
        }
    }
    const BASE: &str = "10 1 253:0 / /var/lib/nxrs-encrypted rw,nosuid,nodev,noexec,relatime - ext4 /dev/mapper/nxrs rw\n";
    #[test]
    fn kernel_mount_policy_rejects_fallback_aliases_and_nested_plaintext() {
        let v = volume();
        assert_eq!(mount_device(BASE, &v), Ok("253:0"));
        for invalid in [String::new(), BASE.replace("ext4", "tmpfs"), BASE.replace("253:0", "bad:0"),
            BASE.replace("noexec,", ""), BASE.replace("nosuid,", ""), BASE.replace("nodev,", ""),
            BASE.replace("rw,nosuid", "ro,nosuid"), BASE.replace(" / /var", " /bind /var"),
            format!("{BASE}{BASE}"),
            format!("{BASE}11 10 0:1 / /var/lib/nxrs-encrypted/activity rw,nosuid,nodev,noexec - tmpfs tmpfs rw\n"),
            format!("{BASE}11 10 253:1 / /var/lib/nxrs-encrypted/activity rw,nosuid,nodev,noexec - ext4 /dev/mapper/other rw\n")]
        { assert_eq!(mount_device(&invalid, &v), Err(Error::Protection)); }
        let same = format!("{BASE}11 10 253:0 /activity /var/lib/nxrs-encrypted/activity rw,nosuid,nodev,noexec - ext4 /dev/mapper/nxrs rw\n");
        assert_eq!(mount_device(&same, &v), Ok("253:0"));
    }
    #[test]
    fn kernel_identity_requires_the_exact_luks2_uuid() {
        let v = volume();
        let correct = "CRYPT-LUKS2-0123456789abcdef0123456789abcdef-nxrs\n";
        assert_eq!(identity(correct, &v), Ok(()));
        for bad in [
            correct.replace("LUKS2", "LUKS1"),
            correct.replace("01234567", "11234567"),
            "CRYPT-PLAIN-nxrs".into(),
            correct.replace("-nxrs", "-"),
            correct.replace("nxrs", "nxrs\nother"),
        ] {
            assert_eq!(identity(&bad, &v), Err(Error::Protection));
        }
    }
}
